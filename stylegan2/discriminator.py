import numpy as np
import tensorflow as tf

from stylegan2.custom_layers import Dense, ResizeConv2D, Bias, LeakyReLU, MinibatchStd


class FromRGB(tf.keras.layers.Layer):
    def __init__(self, fmaps, **kwargs):
        super(FromRGB, self).__init__(**kwargs)
        self.fmaps = fmaps

        self.conv = ResizeConv2D(fmaps=self.fmaps, kernel=1, gain=1.0, lrmul=1.0,
                                 up=False, down=False, resample_kernel=None, name='conv')
        self.apply_bias = Bias(lrmul=1.0, name='bias')
        self.leaky_relu = LeakyReLU(name='lrelu')

    def call(self, inputs, training=None, mask=None):
        y = self.conv(inputs)
        y = self.apply_bias(y)
        y = self.leaky_relu(y)
        return y


class DiscriminatorBlock(tf.keras.layers.Layer):
    def __init__(self, n_f0, n_f1, res, **kwargs):
        super(DiscriminatorBlock, self).__init__(**kwargs)
        self.gain = 1.0
        self.lrmul = 1.0
        self.n_f0 = n_f0
        self.n_f1 = n_f1
        self.res = res
        self.resnet_scale = 1. / np.sqrt(2.)

        # conv_0
        self.conv_0 = ResizeConv2D(fmaps=self.n_f0, kernel=3, gain=self.gain, lrmul=self.lrmul,
                                   up=False, down=False, resample_kernel=None, name='conv_0')
        self.apply_bias_0 = Bias(self.lrmul, name='bias_0')
        self.leaky_relu_0 = LeakyReLU(name='lrelu_0')

        # conv_1 down
        self.conv_1 = ResizeConv2D(fmaps=self.n_f1, kernel=3, gain=self.gain, lrmul=self.lrmul,
                                   up=False, down=True, resample_kernel=[1, 3, 3, 1], name='conv_1')
        self.apply_bias_1 = Bias(self.lrmul, name='bias_1')
        self.leaky_relu_1 = LeakyReLU(name='lrelu_1')

        # resnet skip
        self.conv_skip = ResizeConv2D(fmaps=self.n_f1, kernel=1, gain=self.gain, lrmul=self.lrmul,
                                      up=False, down=True, resample_kernel=[1, 3, 3, 1], name='skip')

    def call(self, inputs, training=None, mask=None):
        x = inputs
        residual = x

        # conv0
        x = self.conv_0(x)
        x = self.apply_bias_0(x)
        x = self.leaky_relu_0(x)

        # conv1 down
        x = self.conv_1(x)
        x = self.apply_bias_1(x)
        x = self.leaky_relu_1(x)

        # resnet skip
        residual = self.conv_skip(residual)
        x = (x + residual) * self.resnet_scale
        return x


class DiscriminatorLastBlock(tf.keras.layers.Layer):
    def __init__(self, n_f0, n_f1, res, **kwargs):
        super(DiscriminatorLastBlock, self).__init__(**kwargs)
        self.gain = 1.0
        self.lrmul = 1.0
        self.n_f0 = n_f0
        self.n_f1 = n_f1
        self.res = res

        self.minibatch_std = MinibatchStd(group_size=4, num_new_features=1, name='minibatchstd')

        # conv_0
        self.conv_0 = ResizeConv2D(fmaps=self.n_f0, kernel=3, gain=self.gain, lrmul=self.lrmul,
                                   up=False, down=False, resample_kernel=None, name='conv_0')
        self.apply_bias_0 = Bias(self.lrmul, name='bias_0')
        self.leaky_relu_0 = LeakyReLU(name='lrelu_0')

        # dense_1
        self.dense_1 = Dense(self.n_f1, gain=self.gain, lrmul=self.lrmul, name='dense_1')
        self.apply_bias_1 = Bias(self.lrmul, name='bias_1')
        self.leaky_relu_1 = LeakyReLU(name='lrelu_1')

    def call(self, x, training=None, mask=None):
        x = self.minibatch_std(x)

        # conv_0
        x = self.conv_0(x)
        x = self.apply_bias_0(x)
        x = self.leaky_relu_0(x)

        # dense_1
        x = self.dense_1(x)
        x = self.apply_bias_1(x)
        x = self.leaky_relu_1(x)
        return x


class Discriminator(tf.keras.Model):
    def __init__(self, d_params, **kwargs):
        super(Discriminator, self).__init__(**kwargs)
        # discriminator's (resolutions and featuremaps) are reversed against generator's
        self.labels_dim = d_params['labels_dim']
        self.r_resolutions = d_params['resolutions'][::-1]
        self.r_featuremaps = d_params['featuremaps'][::-1]

        # stack discriminator blocks
        res0, n_f0 = self.r_resolutions[0], self.r_featuremaps[0]
        self.initial_fromrgb = FromRGB(fmaps=n_f0, name='{:d}x{:d}/FromRGB'.format(res0, res0))
        self.blocks = list()
        for index, (res0, n_f0) in enumerate(zip(self.r_resolutions[:-1], self.r_featuremaps[:-1])):
            n_f1 = self.r_featuremaps[index + 1]
            self.blocks.append(DiscriminatorBlock(n_f0=n_f0, n_f1=n_f1, res=res0, name='{:d}x{:d}'.format(res0, res0)))

        # set last discriminator block
        res = self.r_resolutions[-1]
        n_f0, n_f1 = self.r_featuremaps[-2], self.r_featuremaps[-1]
        self.last_block = DiscriminatorLastBlock(n_f0, n_f1, res, name='{:d}x{:d}'.format(res, res))

        # set last dense layer
        self.last_dense = Dense(max(self.labels_dim, 1), gain=1.0, lrmul=1.0, name='last_dense')
        self.last_bias = Bias(lrmul=1.0, name='last_bias')

    def call(self, inputs, training=None, mask=None):
        images, labels = inputs

        x = self.initial_fromrgb(images)
        for block in self.blocks:
            x = block(x)

        x = self.last_block(x)
        x = self.last_dense(x)
        x = self.last_bias(x)

        if self.labels_dim > 0:
            x = tf.reduce_sum(x * labels, axis=1, keepdims=True)
        scores_out = x
        return scores_out


def main():
    batch_size = 4
    d_params_with_label = {
        'labels_dim': 0,
        'resolutions': [4, 8, 16, 32, 64, 128, 256, 512, 1024],
        'featuremaps': [512, 512, 512, 512, 512, 256, 128, 64, 32],
    }

    input_res = d_params_with_label['resolutions'][-1]
    test_images = np.ones((batch_size, 3, input_res, input_res), dtype=np.float32)
    test_labels = np.ones((batch_size, d_params_with_label['labels_dim']), dtype=np.float32)

    discriminator = Discriminator(d_params_with_label)
    scores1 = discriminator([test_images, test_labels], training=True)
    scores2 = discriminator([test_images, test_labels], training=False)
    discriminator.summary()

    print(scores1.shape)
    print(scores2.shape)

    print()
    for v in discriminator.variables:
        print('{}: {}'.format(v.name, v.shape))

    return


if __name__ == '__main__':
    main()
