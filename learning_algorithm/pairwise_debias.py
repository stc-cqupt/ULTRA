"""Training and testing the Pairwise Debiasing algorithm for unbiased learning to rank.

See the following paper for more information on the Pairwise Debiasing algorithm.
    
    * Hu, Ziniu, Yang Wang, Qu Peng, and Hang Li. "Unbiased LambdaMART: An Unbiased Pairwise Learning-to-Rank Algorithm." In The World Wide Web Conference, pp. 2830-2836. ACM, 2019.
    
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math
import os
import random
import sys
import time
import numpy as np
import tensorflow as tf
import tensorflow_ranking as tfr
import copy
import itertools
from six.moves import zip
from tensorflow import dtypes

from . import ranking_model
from . import metrics
from .BasicAlgorithm import BasicAlgorithm
sys.path.append("..")
import utils


def get_bernoulli_sample(probs):
    """Conduct Bernoulli sampling according to a specific probability distribution.

        Args:
            prob: (tf.Tensor) A tensor in which each element denotes a probability of 1 in a Bernoulli distribution.

        Returns:
            A Tensor of binary samples (0 or 1) with the same shape of probs.

        """
    return tf.ceil(probs - tf.random_uniform(tf.shape(probs)))

class PairDebias(BasicAlgorithm):
    """The Pairwise Debiasing algorithm for unbiased learning to rank.

    This class implements the Pairwise Debiasing algorithm based on the input layer 
    feed. See the following paper for more information.
    
    * Hu, Ziniu, Yang Wang, Qu Peng, and Hang Li. "Unbiased LambdaMART: An Unbiased Pairwise Learning-to-Rank Algorithm." In The World Wide Web Conference, pp. 2830-2836. ACM, 2019.
    
    """

    def __init__(self, data_set, exp_settings, forward_only=False):
        """Create the model.
    
        Args:
            data_set: (Raw_data) The dataset used to build the input layer.
            exp_settings: (dictionary) The dictionary containing the model settings.
            forward_only: Set true to conduct prediction only, false to conduct training.
        """
        print('Build Pairwise Debiasing algorithm.')

        self.hparams = tf.contrib.training.HParams(
            learning_rate=0.05,                 # Learning rate.
            max_gradient_norm=5.0,            # Clip gradients to this norm.
            regulation_p=2,                 # An int specify the regularization term.
            l2_loss=0.0,                    # Set strength for L2 regularization.
            grad_strategy='ada',            # Select gradient strategy
        )
        print(exp_settings['learning_algorithm_hparams'])
        self.hparams.parse(exp_settings['learning_algorithm_hparams'])
        self.exp_settings = exp_settings

        self.rank_list_size = data_set.rank_list_size
        self.feature_size = data_set.feature_size
        self.learning_rate = tf.Variable(float(self.hparams.learning_rate), trainable=False)
        
        # Feeds for inputs.
        self.is_training = tf.placeholder(tf.bool, name="is_train")
        self.docid_inputs = [] # a list of top documents
        self.letor_features = tf.placeholder(tf.float32, shape=[None, self.feature_size], 
                                name="letor_features") # the letor features for the documents
        self.labels = []  # the labels for the documents (e.g., clicks)
        for i in range(self.rank_list_size):
            self.docid_inputs.append(tf.placeholder(tf.int64, shape=[None],
                                            name="docid_input{0}".format(i)))
            self.labels.append(tf.placeholder(tf.float32, shape=[None],
                                            name="label{0}".format(i)))

        self.global_step = tf.Variable(0, trainable=False)

        # Build model
        self.output = self.ranking_model(forward_only)
        
        reshaped_labels = tf.transpose(tf.convert_to_tensor(self.labels)) # reshape from [rank_list_size, ?] to [?, rank_list_size]
        # Build unbiased pairwise loss only when it is training
        if not forward_only:
            # Build propensity parameters
            self.t_plus = tf.Variable(tf.ones([1, self.rank_list_size]), trainable=False)
            self.t_minus = tf.Variable(tf.ones([1, self.rank_list_size]), trainable=False)
            self.splitted_t_plus = tf.split(self.t_plus, self.rank_list_size, axis=1)
            self.splitted_t_minus = tf.split(self.t_minus, self.rank_list_size, axis=1)
            for i in range(self.rank_list_size):
                tf.summary.scalar('t_plus Probability %d' % i, tf.reduce_max(self.splitted_t_plus[i]), collections=['train'])
                tf.summary.scalar('t_minus Probability %d' % i, tf.reduce_max(self.splitted_t_minus[i]), collections=['train'])

            # Build pairwise loss based on clicks (0 for unclick, 1 for click)
            output_list = tf.split(self.output, self.rank_list_size, axis=1)
            t_plus_loss_list = [0.0 for _ in range(self.rank_list_size)]
            t_minus_loss_list = [0.0 for _ in range(self.rank_list_size)]
            self.loss = 0.0
            for i in range(self.rank_list_size):
                for j in range(self.rank_list_size):
                    if i == j:
                        continue
                    valid_pair_mask = tf.nn.relu(self.labels[i] - self.labels[j])
                    pair_loss = tf.reduce_sum(
                        valid_pair_mask * self.pairwise_cross_entropy_loss(output_list[i], output_list[j])
                    )
                    t_plus_loss_list[i] += pair_loss / self.splitted_t_minus[j]
                    t_minus_loss_list[j] += pair_loss / self.splitted_t_plus[i]
                    self.loss += pair_loss / self.splitted_t_plus[i] / self.splitted_t_minus[j]

            # Update propensity
            # TODO add a learning rate here to avoid unstable EM process with small batches.
            self.update_propensity_op = tf.group(
                self.t_plus.assign(
                    tf.pow(tf.concat(t_minus_loss_list, axis=1) / t_minus_loss_list[0], 1/(self.hparams.regulation_p + 1))
                    ), 
                self.t_minus.assign(
                    tf.pow(tf.concat(t_plus_loss_list, axis=1) / t_plus_loss_list[0], 1/(self.hparams.regulation_p + 1))
                )
            )

            # Add l2 loss
            params = tf.trainable_variables()
            if self.hparams.l2_loss > 0:
                for p in params:
                    self.loss += self.hparams.l2_loss * tf.nn.l2_loss(p)

            # Gradients and SGD update operation for training the model.
            opt = tf.train.AdagradOptimizer(self.hparams.learning_rate)
            self.gradients = tf.gradients(self.loss, params)
            if self.hparams.max_gradient_norm > 0:
                self.clipped_gradients, self.norm = tf.clip_by_global_norm(self.gradients,
                                                                     self.hparams.max_gradient_norm)
                self.updates = opt.apply_gradients(zip(self.clipped_gradients, params),
                                             global_step=self.global_step)
                tf.summary.scalar('Gradient Norm', self.norm, collections=['train'])
            else:
                self.norm = None 
                self.updates = opt.apply_gradients(zip(self.gradients, params),
                                             global_step=self.global_step)
            tf.summary.scalar('Learning Rate', self.learning_rate, collections=['train'])
            tf.summary.scalar('Loss', tf.reduce_mean(self.loss), collections=['train'])
            
        for metric in self.exp_settings['metrics']:
            for topn in self.exp_settings['metrics_topn']:
                metric_value = metrics.make_ranking_metric_fn(metric, topn)(reshaped_labels, self.output, None)
                tf.summary.scalar('%s_%d' % (metric, topn), metric_value, collections=['train', 'eval'])

        self.train_summary = tf.summary.merge_all(key='train')
        self.eval_summary = tf.summary.merge_all(key='eval')
        self.saver = tf.train.Saver(tf.global_variables())

    def ranking_model(self, forward_only=False, scope=None):
        with tf.variable_scope(scope or "ranking_model"):
            PAD_embed = tf.zeros([1,self.feature_size],dtype=tf.float32)
            letor_features = tf.concat(axis=0,values=[self.letor_features, PAD_embed])
            input_feature_list = []
            output_scores = []

            model = utils.find_class(self.exp_settings['ranking_model'])(self.exp_settings['ranking_model_hparams'])

            for i in range(self.rank_list_size):
                input_feature_list.append(tf.nn.embedding_lookup(letor_features, self.docid_inputs[i]))
            output_scores = model.build(input_feature_list, is_training=self.is_training)

            return tf.concat(output_scores,1)

    def step(self, session, input_feed, forward_only):
        """Run a step of the model feeding the given inputs.

        Args:
            session: (tf.Session) tensorflow session to use.
            input_feed: (dictionary) A dictionary containing all the input feed data.
            forward_only: whether to do the backward step (False) or only forward (True).

        Returns:
            A triple consisting of the loss, outputs (None if we do backward),
            and a tf.summary containing related information about the step.

        """
        
        # Output feed: depends on whether we do a backward step or not.
        if not forward_only:
            input_feed[self.is_training.name] = True
            output_feed = [
                            self.updates,    # Update Op that does SGD.
                            self.loss,    # Loss for this batch.
                            self.update_propensity_op,
                            self.train_summary # Summarize statistics.
                            ]    
        else:
            input_feed[self.is_training.name] = False
            output_feed = [
                        self.eval_summary, # Summarize statistics.
                        self.output   # Model outputs
            ]    

        outputs = session.run(output_feed, input_feed)
        if not forward_only:
            return outputs[1], None, outputs[-1]    # loss, no outputs, summary.
        else:
            return None, outputs[1], outputs[0]    # loss, outputs, summary.
