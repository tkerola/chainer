import numpy

from chainer import cuda

from chainer import function
from chainer.utils import type_check


if cuda.cudnn_enabled:
    cudnn = cuda.cudnn
    libcudnn = cudnn.cudnn
    _cudnn_version = libcudnn.getVersion()


def _as4darray(arr):
    if arr.ndim == 0:
        return arr.reshape(1, 1, 1, 1)
    elif arr.ndim == 4:
        return arr
    else:
        return arr.reshape(arr.shape[0], -1, 1, 1)


def _xhat(x, mean, std, expander):
    x_mu = x - mean[expander]
    x_mu /= std[expander]
    return x_mu


class BatchRenormalizationFunction(function.Function):

    def __init__(self, eps=2e-5, mean=None, var=None, train=False,
                 decay=0.9, use_cudnn=True, rmax=1, dmax=0,
                 keep_r_d_fixed=False):
        self.running_mean = mean
        self.running_var = var
        self.rmax = rmax
        self.dmax = dmax
        self.r = None
        self.d = None
        # Needed for gradient check to be possible
        self.keep_r_d_fixed = keep_r_d_fixed

        # If train is true, use batch statistics (training mode). Otherwise, if
        # false, use the supplied mean and variance.
        self.train = train
        # Note: cuDNN v5 requires that eps be greater than 1e-5. Otherwise, an
        # error will occur.
        # See CUDNN_BN_MIN_EPSILON value in cudnn.h to verify minimum allowable
        # value.
        self.eps = eps
        if cuda.cudnn_enabled and use_cudnn:
            if eps < 1e-5:
                msg = 'cuDNN does not allow an eps value less than 1e-5.'
                raise RuntimeError(msg)
        self.use_cudnn = use_cudnn
        self.mean_cache = None
        self.decay = decay

    def check_type_forward(self, in_types):
        n_in = in_types.size().eval()
        if n_in != 3 and n_in != 5:
            raise type_check.InvalidType(
                '%s or %s' % (in_types.size() == 3, in_types.size() == 5),
                '%s == %s' % (in_types.size(), n_in))
        x_type, gamma_type, beta_type = in_types[:3]
        M = gamma_type.ndim.eval()
        type_check.expect(
            x_type.dtype.kind == 'f',
            x_type.ndim >= gamma_type.ndim + 1,
            x_type.shape[1:1 + M] == gamma_type.shape,
            # TODO(beam2d): Check shape
            gamma_type.dtype == x_type.dtype,
            beta_type.dtype == x_type.dtype,
            gamma_type.shape == beta_type.shape,
        )
        if len(in_types) == 5:
            mean_type, var_type = in_types[3:]
            type_check.expect(
                mean_type.dtype == x_type.dtype,
                mean_type.shape == gamma_type.shape,
                var_type.dtype == x_type.dtype,
                var_type.shape == gamma_type.shape,
            )

    def forward(self, inputs):
        xp = cuda.get_array_module(*inputs)
        x, gamma, beta = inputs[:3]
        if self.train:
            if self.running_mean is None:
                print("Set running_mean,var to zero")
                self.running_mean = xp.zeros_like(gamma)
                self.running_var = xp.zeros_like(gamma)
            else:
                self.running_mean = xp.array(self.running_mean)
                self.running_var = xp.array(self.running_var)
        elif len(inputs) == 5:
            self.fixed_mean = inputs[3]
            self.fixed_var = inputs[4]

        # TODO(bkvogel): Check for float16 support again in next cuDNN version.
        if x[0].dtype == numpy.float16:
            # cuDNN v5 batch normalization does not seem to support float16.
            self.use_cudnn = False

        head_ndim = gamma.ndim + 1
        expander = (None, Ellipsis) + (None,) * (x.ndim - head_ndim)

        # cuDNN only supports these tensor dimensions because they are
        # the most commonly used. If there is a need to support other
        # dimensions with cuDNN, we could consider reshaping the input
        # into a 2-dim array with channels as second dim and m=<product
        # of all dimensions except the 2nd dimension> as the first
        # dimension.
        self.cudnn_dim_ok = x.ndim == 2 or x.ndim == 4

        cudnn_updated_running_stats = False
        # NOTE(tommi): Removed cuDNN support since it does not support
        # batch renormalization
        if self.train:
            axis = (0,) + tuple(range(head_ndim, x.ndim))
            mean = x.mean(axis=axis)
            var = x.var(axis=axis)
            var += self.eps
        else:
            mean = self.fixed_mean
            var = self.fixed_var + self.eps
        self.std = xp.sqrt(var, dtype=var.dtype)
        if not self.keep_r_d_fixed or self.r is None:
            if self.train:
                running_sigma = xp.sqrt(self.running_var + self.eps)
                r = xp.clip(self.std / running_sigma,
                            1.0 / self.rmax, self.rmax)
                d = xp.clip((mean - self.running_mean) / running_sigma,
                            -self.dmax, self.dmax)
            else:
                r = xp.ones_like(gamma)
                d = xp.zeros_like(gamma)

            if self.keep_r_d_fixed:
                # Hack for making gradient check treat r and d as true
                # constants with respect to the batch.
                print("Warning: self.keep_r_d_fixed is True, which is needed "
                      "for gradient check, but must be disabled during real "
                      "training")
                print("running_var:", self.running_var)
                print("running_mean:", self.running_mean)
                print("Set r = {}".format(r))
                print("Set d = {}".format(d))
            self.r = r
            self.d = d

        if self.keep_r_d_fixed:
            # Need to explicitly cast during gradient check, as r and d are
            # not updated during finite differences
            self.r = self.r.astype(gamma.dtype)
            self.d = self.d.astype(gamma.dtype)

        gamma = gamma[expander]
        beta = beta[expander]

        if xp is numpy:
            self.x_hat = _xhat(x, mean, self.std, expander)
            self.x_hat_renorm = self.x_hat * self.r[expander] + \
                self.d[expander]
            y = gamma * self.x_hat_renorm
            y += beta
        else:
            self.x_hat, self.x_hat_renorm, y = cuda.elementwise(
                'T x, T mean, T std, T gamma, T beta, T r, T d',
                'T x_hat, T x_hat_renorm, T y',
                '''
                x_hat = (x - mean) / std;
                x_hat_renorm = x_hat * r + d;
                y = gamma * x_hat_renorm + beta;
                ''',
                'bn_fwd')(x, mean[expander], self.std[expander], gamma,
                          beta, self.r[expander], self.d[expander])

        if self.train and (not cudnn_updated_running_stats):
            # Note: If in training mode, the cuDNN forward training function
            # will do this for us, so
            # only run following code if cuDNN was not used.
            # Update running statistics:
            m = x.size // gamma.size
            adjust = m / max(m - 1., 1.)  # unbiased estimation
            self.running_mean *= self.decay
            temp_ar = xp.array(mean)
            temp_ar *= (1 - self.decay)
            self.running_mean += temp_ar
            del temp_ar
            self.running_var *= self.decay
            temp_ar = xp.array(var)
            temp_ar *= (1 - self.decay) * adjust
            self.running_var += temp_ar
            del temp_ar
        return y,

    def backward(self, inputs, grad_outputs):
        x, gamma = inputs[:2]
        gy = grad_outputs[0]
        head_ndim = gamma.ndim + 1
        expander = (None, Ellipsis) + (None,) * (x.ndim - head_ndim)
        m = gamma.dtype.type(x.size // gamma.size)
        axis = (0,) + tuple(range(head_ndim, x.ndim))
        xp = cuda.get_array_module(x)
        if len(inputs) == 5:
            # This case is unlikely to be used in practice and so does not
            # need to be optimized for performance.
            mean = inputs[3]
            var = inputs[4]
            std = xp.sqrt(var, dtype=var.dtype)
            gs = gamma / std
            gbeta = gy.sum(axis=axis)
            x_hat = _xhat(x, mean, std, expander)
            ggamma = (gy * x_hat).sum(axis=axis)
            gmean = -gs * gbeta
            gvar = -0.5 * gamma / var * ggamma
            gx = gs[expander] * gy
            return gx, ggamma, gbeta, gmean, gvar

        # Note: If length of inputs is not 5, we must be in train mode.
        assert self.train
        # NOTE(tommi): Removed cuDNN support since it does not support
        # batch renormalization
        gbeta = gy.sum(axis=axis)
        ggamma = (gy * self.x_hat_renorm).sum(axis=axis)
        gsigma_batch = (gy * self.x_hat).sum(axis=axis)
        if xp is numpy:
            scale = (self.r * gamma / self.std)[expander]
            gx = scale * (gy - (self.x_hat * gsigma_batch[expander] +
                                gbeta[expander]) / m)
        else:
            inv_m = numpy.float32(1) / m
            gx = cuda.elementwise(
                'T gy, T x_hat, T gamma, T std, T gsigma_batch, T gbeta, \
                T inv_m, T r',
                'T gx',
                'gx = (r * gamma / std) * (gy - (x_hat * gsigma_batch + gbeta) * \
                inv_m)',
                'bn_bwd')(gy, self.x_hat, gamma[expander],
                          self.std[expander], gsigma_batch[expander],
                          gbeta[expander], inv_m, self.r[expander])
        return gx, ggamma, gbeta


def batch_renormalization(x, gamma, beta, rmax, dmax, eps=2e-5,
                          running_mean=None, running_var=None, decay=0.9,
                          use_cudnn=True):
    """Batch renormalization function.

    This is an extension of batch normalization, which ensures that the
    training and inference models generate the same outputs that depend on
    individual examples rather than the entire minibatch.

    See: `Batch Renormalization: Towards Reducing Minibatch Dependence in \
          Batch-Normalized Models <https://arxiv.org/abs/1702.03275>`_

    .. seealso:: :class:`links.BatchRenormalization`
    .. seealso:: :func:`functions.BatchNormalization`

    """
    return BatchRenormalizationFunction(eps, running_mean, running_var, True,
                                        decay, use_cudnn, rmax, dmax)(x, gamma,
                                                                      beta)


def fixed_batch_normalization(x, gamma, beta, mean, var, eps=2e-5,
                              use_cudnn=True):
    return BatchRenormalizationFunction(eps, None, None, False, 0.0,
                                        use_cudnn)(x, gamma, beta, mean, var)