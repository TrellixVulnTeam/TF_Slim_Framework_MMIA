# -*- coding=utf-8 -*-
import tensorflow as tf
slim = tf.contrib.slim

MODEL_TYPE_vgg16 = 'vgg16'
MODEL_TYPE_vgg16_no_dilation = 'vgg16_no_dilation'
dice_coff = 0.5
ms_flag = False
FUSE_TYPE_cascade_conv1x1_upsample_sum = 'cascade_conv1x1_upsample_sum'
FUSE_TYPE_cascade_conv1x1_128_upsamle_sum_conv1x1_2 = \
                            'cascade_conv1x1_128_upsamle_sum_conv1x1_2'
FUSE_TYPE_cascade_conv1x1_128_upsamle_concat_conv1x1_2 = \
                            'cascade_conv1x1_128_upsamle_concat_conv1x1_2'
skip_connection_setting = {
    'vgg16': ['conv1_2','conv2_2', 'conv3_3', 'conv4_3', 'fc7'],
    'res50': [],
}


class TaskNetworkModuleV2(object):
    '''
    对比上一个版本，本版本有multi scale 的功能
    '''
    def __init__(self, input_tensors, output_dim, output_shape, arg_sc, name, ms_flag=False):
        last_output = None
        hidden_dim = 128
        rates = [1, 1, 1, 1]
        # regularizer = tf.contrib.layers.l2_regularizer(scale=0.01)
        # 从深层到浅层
        final_output = []
        learnable_merged_flag = True
        different_scale_outputs = []
        with tf.variable_scope(name):
            with slim.arg_scope(arg_sc):
                for idx, input_tensor in enumerate(input_tensors):
                    shape = input_tensor.get_shape().as_list()
                    if last_output is not None:
                        if learnable_merged_flag:
                            alpha = tf.get_variable('alpha_' + str(idx), shape=[], dtype=tf.float32,
                                                    initializer=tf.ones_initializer(),
                                                    regularizer=None)
                            tf.summary.scalar('alpha_' + str(idx), alpha)
                        last_output = tf.image.resize_images(last_output, [shape[1], shape[2]])
                        print('the last output is ', last_output)
                        print('the output is ', input_tensor)
                        if learnable_merged_flag:
                            output = tf.concat([input_tensor, alpha * last_output], axis=-1)
                        else:
                            output = tf.concat([input_tensor, last_output], axis=-1)
                    else:
                        output = input_tensor
                    output = slim.conv2d(output, hidden_dim, kernel_size=1, stride=1,
                                         scope='level_' + str(idx) + '_1x1')
                    output = slim.conv2d(output, hidden_dim, kernel_size=3, stride=1,
                                         scope='level_' + str(idx) + '_3x3')
                    last_output = output
                    different_scale_outputs.append(last_output)
                    # final_output.append(slim.conv2d)
                    final_output.append(tf.image.resize_images(output, output_shape))
                final_output = slim.conv2d(tf.concat(final_output, -1), hidden_dim * len(input_tensors) / 2,
                                           kernel_size=1, stride=1, scope='merged_1x1')
                final_output = slim.conv2d(final_output, hidden_dim * len(input_tensors) / 2,
                                           kernel_size=3, stride=1, scope='merged_3x3')
                final_output = slim.conv2d(final_output, output_dim,
                                           kernel_size=1, stride=1, scope='logits', activation_fn=None,
                                           normalizer_fn=None)
                self.output = final_output
                if ms_flag:
                    self.different_scale_outputs = []
                    with tf.variable_scope('different_scale_logits'):
                        for idx, output in enumerate(different_scale_outputs):
                            cur_scale_output = slim.conv2d(output, output_dim, kernel_size=1, stride=1,
                                                           scope='logits_' + str(idx), activation_fn=None,
                                                           normalizer_fn=None)
                            self.different_scale_outputs.append(cur_scale_output)


class UNet(object):
    def __init__(self, inputs, mask_input, is_training, base_model='vgg16'):
        self.inputs = inputs
        self.is_training = is_training
        self.mask_input = mask_input
        self.base_model = base_model
        self._build_network()
        self._up_down_layers()
        self._logits_to_scores()

    def _build_network(self):
        import config
        if config.model_type == MODEL_TYPE_vgg16:
            from nets import vgg
            with slim.arg_scope([slim.conv2d],
                                activation_fn=tf.nn.relu,
                                weights_regularizer=slim.l2_regularizer(config.weight_decay),
                                weights_initializer=tf.contrib.layers.xavier_initializer(),
                                biases_initializer=tf.zeros_initializer()):
                with slim.arg_scope([slim.conv2d, slim.max_pool2d],
                                    padding='SAME') as sc:
                    self.arg_scope = sc
                    self.net, self.end_points = vgg.basenet(
                        inputs=self.inputs, pooling='MAX')

        elif config.model_type == MODEL_TYPE_vgg16_no_dilation:
            from nets import vgg
            with slim.arg_scope([slim.conv2d],
                                activation_fn=tf.nn.relu,
                                weights_regularizer=slim.l2_regularizer(config.weight_decay),
                                weights_initializer=tf.contrib.layers.xavier_initializer(),
                                biases_initializer=tf.zeros_initializer()):
                with slim.arg_scope([slim.conv2d, slim.max_pool2d],
                                    padding='SAME') as sc:
                    self.arg_scope = sc
                    self.net, self.end_points = vgg.basenet(
                        inputs=self.inputs, dilation=False, pooling='MAX')
        else:
            raise ValueError('model_type not supported:%s' % (config.model_type))

    def _score_layer(self, input_layer, num_classes, scope):
        import config
        with slim.arg_scope(self.arg_scope):
            logits = slim.conv2d(input_layer, num_classes, [1, 1],
                                 stride=1,
                                 activation_fn=None,
                                 scope='score_from_%s' % scope,
                                 normalizer_fn=None)
            try:
                use_dropout = config.dropout_ratio > 0
            except:
                use_dropout = False

            if use_dropout:
                if self.is_training:
                    dropout_ratio = config.dropout_ratio
                else:
                    dropout_ratio = 0
                keep_prob = 1.0 - dropout_ratio
                tf.logging.info('Using Dropout, with keep_prob = %f' % (keep_prob))
                logits = tf.nn.dropout(logits, keep_prob)
            return logits

    def _upsampleing_layer(self, layer, target_layer):
        #             target_shape = target_layer.shape[1:-1] # NHWC
        target_shape = tf.shape(target_layer)[1:-1]
        upscored = tf.image.resize_images(layer, target_shape)
        return upscored

    def _fuse_by_cascade_conv1x1_128_upsamle_sum_conv1x1_2(self, scope):
        """
        The feature fuse fashion of
            'Deep Direct Regression for Multi-Oriented Scene Text Detection'

        Instead of fusion of scores, feature map from 1x1, 128 conv are fused,
        and the scores are predicted on it.
        """
        base_map = self._fuse_by_cascade_conv1x1_upsample_sum(num_classes=128,
                                                              scope='feature_fuse')
        return base_map

    def _fuse_by_cascade_conv1x1_128_upsamle_concat_conv1x1_2(self, scope, num_classes=32):
        import config
        num_layers = len(config.feat_layers)

        with tf.variable_scope(scope):
            smaller_score_map = None
            for idx in range(0, len(config.feat_layers))[::-1]:  # [4, 3, 2, 1, 0]
                current_layer_name = config.feat_layers[idx]
                current_layer = self.end_points[current_layer_name]
                current_score_map = self._score_layer(current_layer,
                                                      num_classes, current_layer_name)
                if smaller_score_map is None:
                    smaller_score_map = current_score_map
                else:
                    upscore_map = self._upscore_layer(smaller_score_map, current_score_map)
                    smaller_score_map = tf.concat([current_score_map, upscore_map], axis=0)

        return smaller_score_map

    def _fuse_by_cascade_conv1x1_upsample_sum(self, num_classes, scope):
        """
        The feature fuse fashion of FCN for semantic segmentation:
        Suppose there are several feature maps with decreasing sizes ,
        and we are going to get a single score map from them.

        Every feature map contributes to the final score map:
            predict score on all the feature maps using 1x1 conv, with
            depth equal to num_classes

        The score map is upsampled and added in a cascade way:
            start from the smallest score map, upsmale it to the size
            of the next score map with a larger size, and add them
            to get a fused score map. Upsample this fused score map and
            add it to the next sibling larger score map. The final
            score map is got when all score maps are fused together
        """
        import config
        num_layers = len(config.feat_layers)

        with tf.variable_scope(scope):
            smaller_score_map = None
            for idx in range(0, len(config.feat_layers))[::-1]:  # [4, 3, 2, 1, 0]
                current_layer_name = config.feat_layers[idx]
                current_layer = self.end_points[current_layer_name]
                current_score_map = self._score_layer(current_layer,
                                                      num_classes, current_layer_name)
                if smaller_score_map is None:
                    smaller_score_map = current_score_map
                else:
                    upscore_map = self._upscore_layer(smaller_score_map, current_score_map)
                    smaller_score_map = current_score_map + upscore_map

        return smaller_score_map

    def _up_down_layers(self):
        import config
        input_tensors = []
        for idx in range(0, len(skip_connection_setting[self.base_model]))[::-1]:  # [4, 3, 2, 1, 0]
            print('basemode: ', self.base_model)
            current_layer_name = skip_connection_setting[self.base_model][idx]
            current_layer = self.end_points[current_layer_name]
            input_tensors.append(current_layer)
        stride = 1
        img_size = 256
        pixel_cls_modify_module = TaskNetworkModuleV2(input_tensors, config.num_classes,
                                                    [img_size / stride, img_size / stride],
                                                    self.arg_scope, name='pixel_cls_modify', ms_flag=ms_flag)

        self.pixel_cls_logits = pixel_cls_modify_module.output
        if stride != 1:
            self.pixel_cls_logits = tf.image.resize_images(self.pixel_cls_logits, [img_size, img_size])
        if ms_flag:
            self.pixel_cls_logits_ms = pixel_cls_modify_module.different_scale_outputs

    def _flat_pixel_cls_values(self, values):
        shape = values.shape.as_list()
        values = tf.reshape(values, shape=[shape[0], -1, shape[-1]])
        return values

    def _logits_to_scores(self):
        self.pixel_cls_scores = tf.nn.softmax(self.pixel_cls_logits)
        if ms_flag:
            self.pixel_cls_scores_ms = [tf.nn.softmax(ele) for ele in self.pixel_cls_logits_ms]

        tf.summary.image('pred_mask', tf.expand_dims(self.pixel_cls_scores[:, :, :, 1], axis=3) * 200.0, max_outputs=1)
        tf.summary.image('image', self.inputs, max_outputs=1)
        if self.mask_input is not None:
            tf.summary.image('mask', self.mask_input * 200, max_outputs=1)
        self.pixel_cls_logits_flatten = \
            self._flat_pixel_cls_values(self.pixel_cls_logits)
        if ms_flag:
            self.pixel_cls_logits_ms_flatten = [self._flat_pixel_cls_values(ele) for ele in self.pixel_cls_logits_ms]
        self.pixel_cls_scores_flatten = \
            self._flat_pixel_cls_values(self.pixel_cls_scores)
        if ms_flag:
            self.pixel_cls_scores_ms_flatten = [self._flat_pixel_cls_values(ele) for ele in self.pixel_cls_scores_ms]

    def build_cls_loss(self, batch_size, pos_mask, neg_mask, pos_mask_flatten, neg_mask_flatten, n_pos, do_summary, pixel_cls_loss_weight_lambda):
        from OHEM import OHNM_batch
        from losses import loss_with_binary_dice
        with tf.name_scope('pixel_cls_loss'):
            def no_pos():
                return tf.constant(.0)

            def has_pos():
                print('the pixel_cls_logits_flatten is ', self.pixel_cls_logits_flatten)
                print('the pos_mask is ', pos_mask_flatten)
                pixel_cls_loss = tf.nn.sparse_softmax_cross_entropy_with_logits(
                    logits=self.pixel_cls_logits_flatten,
                    labels=tf.cast(pos_mask_flatten, dtype=tf.int32))

                # pixel_neg_scores = self.pixel_cls_scores_flatten[:, :, 0]
                # selected_neg_pixel_mask = OHNM_batch(batch_size, pixel_neg_scores, pos_mask_flatten, neg_mask_flatten)
                #
                # cur_pixel_cls_weights = tf.cast(selected_neg_pixel_mask, tf.float32)
                # n_neg = tf.cast(tf.reduce_sum(selected_neg_pixel_mask), tf.float32)
                # loss = tf.reduce_sum(pixel_cls_loss * cur_pixel_cls_weights) / (n_neg + n_pos)

                # return loss
                return tf.reduce_mean(pixel_cls_loss)

            pixel_cls_loss = has_pos()
            pixel_cls_dice_loss, pixel_cls_dice = loss_with_binary_dice(self.pixel_cls_scores, pos_mask, axis=[1, 2])
            tf.add_to_collection(tf.GraphKeys.LOSSES, pixel_cls_loss * pixel_cls_loss_weight_lambda)
            tf.add_to_collection(tf.GraphKeys.LOSSES, pixel_cls_dice_loss * pixel_cls_loss_weight_lambda * dice_coff)
        return pixel_cls_loss, pixel_cls_dice, pixel_cls_dice_loss

    def build_loss(self, do_summary=True):
        """
        The loss consists of two parts: pixel_cls_loss + link_cls_loss,
            and link_cls_loss is calculated only on positive pixels
        """
        import config

        count_warning = tf.get_local_variable(
            name='count_warning', initializer=tf.constant(0.0))
        batch_size = config.batch_size_per_gpu
        background_label = config.background_label
        text_label = config.text_label
        pixel_link_neg_loss_weight_lambda = config.pixel_link_neg_loss_weight_lambda
        pixel_cls_loss_weight_lambda = config.pixel_cls_loss_weight_lambda


        # build the cls loss
        mask_input = tf.split(self.mask_input, num_or_size_splits=3, axis=-1)
        pos_mask = tf.squeeze(tf.equal(mask_input[1], text_label))
        pos_mask_flatten = tf.reshape(pos_mask, [batch_size, -1])
        neg_mask = tf.squeeze(tf.equal(mask_input[1], background_label))
        neg_mask_flatten = tf.reshape(neg_mask, [batch_size, -1])
        print('the pos_mask=', pos_mask)
        print('the neg_mask=', neg_mask)
        n_pos = tf.reduce_sum(tf.cast(pos_mask, dtype=tf.float32))
        pixel_cls_loss, pixel_cls_dice, pixel_cls_dice_loss = self.build_cls_loss(batch_size, pos_mask, neg_mask,
                                                                                  pos_mask_flatten, neg_mask_flatten,
                                                                                  n_pos, do_summary,
                                                                                  pixel_cls_loss_weight_lambda)

        if do_summary:
            tf.summary.scalar('pixel_cls_loss', pixel_cls_loss)
            tf.summary.scalar('pixel_cls_dice', pixel_cls_dice)
            tf.summary.scalar('pixel_cls_dice_loss', pixel_cls_dice_loss)