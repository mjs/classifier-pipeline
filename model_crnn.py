"""
Author: Matthew Aitchison
Date: Jan 2018
"""

import logging

import numpy as np
import tensorflow as tf

from ml_tools import tools
from ml_tools.model import Model

class ConvModel(Model):
    """
    Base class for convolutional models.
    """

    def conv_layer(self, name, input_layer, filters, kernal_size, conv_stride=1, pool_stride=1, disable_norm=False):
        """ Adds a convolutional layer to the model. """

        tf.summary.histogram(name + '/input', input_layer)
        conv = tf.layers.conv2d(inputs=input_layer, filters=filters, kernel_size=kernal_size,
                                strides=(conv_stride, conv_stride),
                                padding="same", activation=None,
                                name=name + '/conv')

        conv_weights = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, name + '/conv/kernel')[0]

        tf.summary.histogram(name + '/conv_output', conv)
        tf.summary.histogram(name + '/weights', conv_weights)

        activation = tf.nn.relu(conv, name=name + '/relu')
        tf.summary.histogram(name + '/activations', activation)

        if self.params['batch_norm'] and not disable_norm:
            out = tf.layers.batch_normalization(
                activation, fused=True,
                training=self.is_training,
                name=name + "/batchnorm"
            )

            moving_mean = tf.contrib.framework.get_variables(name + '/batchnorm/moving_mean')[0]
            moving_variance = tf.contrib.framework.get_variables(name + '/batchnorm/moving_variance')[0]

            tf.summary.histogram(name + '/batchnorm/mean', moving_mean)
            tf.summary.histogram(name + '/batchnorm/var', moving_variance)
            tf.summary.histogram(name + '/norm_output', out)
        else:
            out = activation

        if pool_stride != 1:
            out = tf.layers.max_pooling2d(inputs=out, pool_size=[pool_stride, pool_stride],
                                          strides=pool_stride,
                                          name=name + "/max_pool"
                                          )
        return out

    def process_inputs(self):
        """ process input channels, returns thermal, flow, mask, """

        # Setup placeholders
        self.X = tf.placeholder(tf.float32, [None, None, 5, 48, 48], name='X')  # [B, F, C, H, W]
        self.y = tf.placeholder(tf.int64, [None], name='y')
        batch_size = tf.shape(self.X)[0]

        # State input allows for processing longer sequences
        zero_state = tf.zeros(shape=[batch_size, self.params['lstm_units'], 2], dtype=tf.float32)
        self.state_in = tf.placeholder_with_default(input=zero_state, shape=[None, self.params['lstm_units'], 2],
                                                    name='state_in')

        # Create some placeholder variables with defaults if not specified
        self.keep_prob = tf.placeholder_with_default(tf.constant(1.0, tf.float32), [], name='keep_prob')
        self.is_training = tf.placeholder_with_default(tf.constant(False, tf.bool), [], name='training')
        self.global_step = tf.placeholder_with_default(tf.constant(0, tf.int32), [], name='global_step')

        # Apply pre-processing
        X = self.X  # [B, F, C, H, W]

        # normalise the thermal
        # the idea here is to apply sqrt to any values over 100 so that we reduce the effect of very strong values.
        thermal = X[:, :, 0:0 + 1]

        AUTO_NORM_THERMAL = False
        THERMAL_ROLLOFF = 400

        if AUTO_NORM_THERMAL:
            thermal = thermal - tf.reduce_mean(thermal, axis=(3, 4), keepdims=True)  # center data
            signs = tf.sign(thermal)
            abs = tf.abs(thermal)
            thermal = tf.minimum(tf.sqrt(abs / THERMAL_ROLLOFF) * THERMAL_ROLLOFF, abs) * signs  # curve off the really strong values
            thermal = thermal - tf.reduce_mean(thermal, axis=(3, 4), keepdims=True)  # center data
            thermal = thermal / tf.sqrt(tf.reduce_mean(tf.square(thermal), axis=(3, 4), keepdims=True))
            # theshold out the background, not sure this is a great idea.  1.5 keeps std approx 1.
            # relu_threshold = +0.1
            # thermal = (tf.nn.relu(thermal - relu_threshold) + relu_threshold) * 1.5
        else:
            signs = tf.sign(thermal)
            abs = tf.abs(thermal)
            thermal = tf.minimum(tf.sqrt(abs / THERMAL_ROLLOFF) * THERMAL_ROLLOFF, abs) * signs  # curve off the really strong values
            thermal = tf.nn.relu(thermal - self.params['thermal_threshold']) + self.params['thermal_threshold']
            thermal = thermal / 40

        # normalise the flow
        # horizontal and vertical flow have different normalisation constants
        flow = X[:, :, 2:3 + 1]
        flow = flow * np.asarray([2.5, 5])[np.newaxis, np.newaxis, :, np.newaxis, np.newaxis]

        # grab the mask
        mask = X[:, :, 4:4 + 1]

        # tap the outputs
        tf.identity(thermal, 'thermal_out')
        tf.identity(flow, 'flow_out')
        tf.identity(mask, 'mask_out')

        # First put all frames in batch into one line sequence, this is required for convolutions.
        # note: we also switch to BHWC format, which is not great, but is required for CPU processing for some reason.
        thermal = tf.transpose(thermal, (0, 1, 3, 4, 2))  # [B, F, H, W, 1]
        flow = tf.transpose(flow, (0, 1, 3, 4, 2))  # [B, F, H, W, 2]

        thermal = tf.reshape(thermal, [-1, 48, 48, 1])  # [B*F, 48, 48, 1]
        flow = tf.reshape(flow, [-1, 48, 48, 2])  # [B*F, 48, 48, 2]

        mask = tf.reshape(mask, [-1, 48, 48, 1])  # [B*F, 48, 48, 1]

        # save distribution of inputs
        self.save_input_summary(thermal, 'inputs/thermal', 3)
        self.save_input_summary(flow[:, :, :, 0:0 + 1], 'inputs/flow/h', 3)
        self.save_input_summary(flow[:, :, :, 1:1 + 1], 'inputs/flow/v', 3)
        self.save_input_summary(mask, 'inputs/mask', 1)

        return thermal, flow, mask

    def setup_novelty(self, logits, hidden):
        """ Creates nodes required for novelty"""

        # samples is [1000, C]
        # logits is [N, C]
        # delta is [N, 1000, C]
        # distances is [N, 1000]

        _, label_count = logits.shape
        _, hidden_count = hidden.shape

        sample_logits = self.create_writable_variable("sample_logits", [1000, label_count])
        self.create_writable_variable("sample_hidden", [1000, hidden_count])

        novelty_threshold = self.create_writable_variable("novelty_threshold", [])
        novelty_scale = self.create_writable_variable("novelty_scale", [])

        delta = tf.expand_dims(logits, axis=1) - tf.expand_dims(sample_logits, axis=0)
        squared_distances = tf.reduce_sum(tf.square(delta), axis=2)
        min_distance = tf.sqrt(tf.reduce_min(squared_distances, axis = 1), name='novelty_distance')
        novelty = tf.sigmoid((min_distance - novelty_threshold) / novelty_scale,'novelty')

        return novelty

    def setup_optimizer(self, loss):
        # setup our training loss
        if self.params['learning_rate_decay'] != 1.0:
            learning_rate = tf.train.exponential_decay(self.params['learning_rate'], self.global_step, 1000,
                                                       self.params['learning_rate_decay'],
                                                       staircase=True)
            tf.summary.scalar('params/learning_rate', learning_rate)
        else:
            learning_rate = self.params['learning_rate']

        # setup optimizer
        optimizer = tf.train.AdamOptimizer(learning_rate=learning_rate, name='Adam')
        update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
        with tf.control_dependencies(update_ops):
            train_op = optimizer.minimize(loss, name='train_op')
            # get gradients
            # note: I can't write out the grads because of problems with NaN
            # his is very concerning as it implies we have a critical problem with training.  Maybe I should try
            # clipping gradients at something very high, say 100?
            # grads = optimizer.compute_gradients(loss)
            # for index, grad in enumerate(grads):
            #    self.create_summaries("grads/{}".format(grads[index][1].name.split(':')[0]), grads[index])


class ModelCRNN_HQ(ConvModel):
    """
    Convolutional neural net model feeding into an LSTM
    """

    MODEL_NAME = "model_hq"
    MODEL_DESCRIPTION = "CNN + LSTM"

    DEFAULT_PARAMS = {

        # training params
        'batch_size': 16,
        'learning_rate': 1e-4,
        'learning_rate_decay': 1.0,
        'l2_reg': 0.0,
        'label_smoothing': 0.1,
        'keep_prob': 0.5,

        # model params
        'batch_norm': True,
        'lstm_units': 512,
        'enable_flow': True,

        # augmentation
        'augmentation': True,
        'thermal_threshold': 10,
        'scale_frequency': 0.5
    }

    def __init__(self, labels, **kwargs):
        """
        Initialise the model
        :param labels: number of labels for model to predict
        """
        super().__init__()
        self.params.update(self.DEFAULT_PARAMS)
        self.params.update(kwargs)
        self._build_model(labels)


    def _build_model(self, label_count):
        ####################################
        # CNN + LSTM
        # based on https://arxiv.org/pdf/1507.06527.pdf
        ####################################
        
        # dimensions are documents as follows
        # B batch size
        # F frames per segment
        # C channels 
        # H frame height
        # W frame width

        thermal, flow, mask = self.process_inputs()
        frame_count = tf.shape(self.X)[1]

        # -------------------------------------
        # run the Convolutions

        layer = thermal

        layer = self.conv_layer('thermal/1', layer, 64, [3, 3], pool_stride=2)
        layer = self.conv_layer('thermal/2', layer, 64, [3, 3], pool_stride=2)
        layer = self.conv_layer('thermal/3', layer, 96, [3, 3], pool_stride=2)
        layer = self.conv_layer('thermal/4', layer, 128, [3, 3], pool_stride=2)
        layer = self.conv_layer('thermal/5', layer, 128, [3, 3], pool_stride=1)

        filtered_conv = layer
        filtered_out = tf.reshape(filtered_conv, [-1, frame_count, tools.product(filtered_conv.shape[1:])],
                                  name='thermal/out')
        logging.info("Thermal convolution output shape: {}".format(filtered_conv.shape))

        if self.params['enable_flow']:
            # integrate thermal and flow into a 3 channel layer
            layer = tf.concat((thermal, flow), axis=3)
            layer = self.conv_layer('motion/1', layer, 64, [3, 3], pool_stride=2)
            layer = self.conv_layer('motion/2', layer, 64, [3, 3], pool_stride=2)
            layer = self.conv_layer('motion/3', layer, 96, [3, 3], pool_stride=2)
            layer = self.conv_layer('motion/4', layer, 128, [3, 3], pool_stride=2)
            layer = self.conv_layer('motion/5', layer, 128, [3, 3], pool_stride=1)

            motion_conv = layer
            motion_out = tf.reshape(motion_conv, [-1, frame_count, tools.product(motion_conv.shape[1:])], name='motion/out')
            logging.info("Motion convolution output shape: {}".format(motion_conv.shape))

            out = tf.concat((filtered_out, motion_out), axis=2, name='out')
        else:
            out = tf.concat((filtered_out,), axis=2, name='out')

        logging.info('Output shape {}'.format(out.shape))

        # -------------------------------------
        # run the LSTM
        lstm_cell = tf.nn.rnn_cell.LSTMCell(num_units=self.params['lstm_units'])
        dropout = tf.nn.rnn_cell.DropoutWrapper(
            lstm_cell,
            output_keep_prob=self.keep_prob,
            dtype=np.float32)
        init_state = tf.nn.rnn_cell.LSTMStateTuple(self.state_in[:,:,0], self.state_in[:,:,1])

        lstm_outputs, lstm_states = tf.nn.dynamic_rnn(
            cell=dropout, inputs=out,
            initial_state=init_state,
            dtype=tf.float32,
            scope='lstm'
        )

        lstm_state_1, lstm_state_2 = lstm_states

        # just need the last output
        lstm_output = tf.identity(lstm_outputs[:, -1], 'lstm_out')
        lstm_state = tf.stack([lstm_state_1, lstm_state_2], axis=2)

        logging.info("lstm output shape: {} x {}".format(lstm_outputs.shape[1], lstm_output.shape))
        logging.info("lstm state shape: {}".format(lstm_state.shape))

        if self.params['l2_reg'] > 0:
            regularizer = tf.contrib.layers.l2_regularizer(scale=self.params['l2_reg'])
        else:
            regularizer = None


        # dense hidden layer
        dense = tf.layers.dense(inputs=lstm_output, units=384, activation=tf.nn.relu, name='hidden',
                                kernel_regularizer=regularizer)

        dense = tf.nn.dropout(dense, keep_prob=self.keep_prob)

        # dense layer on top of convolutional output mapping to class labels.
        logits = tf.layers.dense(inputs=dense, units=label_count, activation=None, name='logits',
                                 kernel_regularizer=regularizer)

        tf.summary.histogram('weights/dense', dense)
        tf.summary.histogram('weights/logits', logits)

        # loss
        softmax_loss = tf.losses.softmax_cross_entropy(
            onehot_labels=tf.one_hot(self.y, label_count),
            logits=logits, label_smoothing=self.params['label_smoothing'],
            scope='softmax_loss')

        if self.params['l2_reg'] != 0:
            reg_loss = tf.losses.get_regularization_loss()
            loss = tf.add(softmax_loss, reg_loss, name='loss')
            tf.summary.scalar('loss/reg', reg_loss)
            tf.summary.scalar('loss/softmax', softmax_loss)
        else:
            # just relabel the loss node
            loss = tf.identity(softmax_loss, name='loss')

        class_out = tf.argmax(logits, axis=1, name='class_out')
        correct_prediction = tf.equal(class_out, self.y)
        pred = tf.nn.softmax(logits, name='prediction')
        accuracy = tf.reduce_mean(tf.cast(correct_prediction, dtype=tf.float32), name='accuracy')

        # -------------------------------------
        # novelty

        self.setup_novelty(logits, lstm_output)
        self.setup_optimizer(loss)

        # make reference to special nodes
        tf.identity(lstm_state, 'state_out')
        tf.identity(dense, 'hidden_out')
        tf.identity(logits, 'logits_out')

        self.attach_nodes()

class ModelCRNN_LQ(ConvModel):
    """
    Convolutional neural net model feeding into an LSTM

    Lower quality, but faster model
    Uses 256 LSTM units and conv stride instead of max pool
    Uses less filters

    Trains on GPU at around 5ms / segment as apposed to 16ms for the high quality model.
    """

    MODEL_NAME = "model_lq"
    MODEL_DESCRIPTION = "CNN + LSTM"

    DEFAULT_PARAMS = {

        # training params
        'batch_size': 16,
        'learning_rate': 1e-4,
        'learning_rate_decay': 1.0,
        'l2_reg': 0,
        'label_smoothing': 0.1,
        'keep_prob': 0.2,

        # model params
        'batch_norm': True,
        'lstm_units': 256,
        'enable_flow': True,

        # augmentation
        'augmentation': True,
        'thermal_threshold': 10,
        'scale_frequency': 0.5
    }

    def __init__(self, labels, **kwargs):
        """
        Initialise the model
        :param labels: number of labels for model to predict
        """
        super().__init__()
        self.params.update(self.DEFAULT_PARAMS)
        self.params.update(kwargs)
        self._build_model(labels)

    def _build_model(self, label_count):
        ####################################
        # CNN + LSTM
        # based on https://arxiv.org/pdf/1507.06527.pdf
        ####################################

        # dimensions are documents as follows
        # B batch size
        # F frames per segment
        # C channels
        # H frame height
        # W frame width

        thermal, flow, mask = self.process_inputs()
        frame_count = tf.shape(self.X)[1]

        # -------------------------------------
        # run the Convolutions

        layer = thermal
        layer = self.conv_layer('thermal/1', layer, 32, [3, 3], conv_stride=2)
        layer = self.conv_layer('thermal/2', layer, 48, [3, 3], conv_stride=2)
        layer = self.conv_layer('thermal/3', layer, 64, [3, 3], conv_stride=2)
        layer = self.conv_layer('thermal/4', layer, 64, [3, 3], conv_stride=2)
        layer = self.conv_layer('thermal/5', layer, 64, [3, 3], conv_stride=1)

        filtered_conv = layer
        logging.info("Thermal convolution output shape: {}".format(filtered_conv.shape))
        filtered_out = tf.reshape(filtered_conv, [-1, frame_count, tools.product(filtered_conv.shape[1:])],
                                  name='thermal/out')

        if self.params['enable_flow']:
            # integrate thermal and flow into a 3 channel layer
            layer = tf.concat((thermal, flow), axis=3)
            layer = self.conv_layer('motion/1', layer, 32, [3, 3], conv_stride=2)
            layer = self.conv_layer('motion/2', layer, 48, [3, 3], conv_stride=2)
            layer = self.conv_layer('motion/3', layer, 64, [3, 3], conv_stride=2)
            layer = self.conv_layer('motion/4', layer, 64, [3, 3], conv_stride=2)
            layer = self.conv_layer('motion/5', layer, 64, [3, 3], conv_stride=1)

            motion_conv = layer
            logging.info("Motion convolution output shape: {}".format(motion_conv.shape))
            motion_out = tf.reshape(motion_conv, [-1, frame_count, tools.product(motion_conv.shape[1:])],
                                    name='motion/out')

            out = tf.concat((filtered_out, motion_out), axis=2, name='out')
        else:
            out = tf.concat((filtered_out, ), axis=2, name='out')

        logging.info('Output shape {}'.format(out.shape))

        # -------------------------------------
        # run the LSTM

        lstm_cell = tf.nn.rnn_cell.LSTMCell(num_units=self.params['lstm_units'])
        dropout = tf.nn.rnn_cell.DropoutWrapper(lstm_cell, output_keep_prob=self.keep_prob, dtype=np.float32)
        init_state = tf.nn.rnn_cell.LSTMStateTuple(self.state_in[:, :, 0], self.state_in[:, :, 1])

        lstm_outputs, lstm_states = tf.nn.dynamic_rnn(
            cell=dropout, inputs=out,
            initial_state=init_state,
            dtype=tf.float32,
            scope='lstm'
        )

        lstm_state_1, lstm_state_2 = lstm_states

        # just need the last output
        lstm_output = tf.identity(lstm_outputs[:, -1], 'lstm_out')
        lstm_state = tf.stack([lstm_state_1, lstm_state_2], axis=2)

        logging.info("lstm output shape: {} x {}".format(lstm_outputs.shape[1], lstm_output.shape))
        logging.info("lstm state shape: {}".format(lstm_state.shape))

        # -------------------------------------
        # dense / logits

        # dense layer on top of convolutional output mapping to class labels.
        logits = tf.layers.dense(inputs=lstm_output, units=label_count, activation=None, name='logits')
        tf.summary.histogram('weights/logits', logits)

        softmax_loss = tf.losses.softmax_cross_entropy(
            onehot_labels=tf.one_hot(self.y, label_count),
            logits=logits, label_smoothing=self.params['label_smoothing'],
            scope='softmax_loss')

        if self.params['l2_reg'] != 0:
            with tf.variable_scope('logits', reuse=True):
                logit_weights = tf.get_variable('kernel')

            reg_loss = (tf.nn.l2_loss(logit_weights, name='loss/reg') * self.params['l2_reg'])
            loss = tf.add(
                softmax_loss, reg_loss, name='loss'
            )
            tf.summary.scalar('loss/reg', reg_loss)
            tf.summary.scalar('loss/softmax', softmax_loss)
        else:
            # just relabel the loss node
            loss = tf.identity(softmax_loss, 'loss')

        class_out = tf.argmax(logits, axis=1, name='class_out')
        correct_prediction = tf.equal(class_out, self.y)
        pred = tf.nn.softmax(logits, name='prediction')
        accuracy = tf.reduce_mean(tf.cast(correct_prediction, dtype=tf.float32), name='accuracy')

        self.setup_novelty(logits, lstm_output)
        self.setup_optimizer(loss)

        # make reference to special nodes
        tf.identity(lstm_state, 'state_out')
        tf.identity(lstm_output, 'hidden_out')
        tf.identity(logits, 'logits_out')
        self.attach_nodes()
