# coding: utf-8

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import copy
import tensorflow as tf

import func
from models import model
from modules import speech
from utils import util, dtype


def stacking(inputs, scale=3, mask=None):
    """
    Reshapes the given outputs, i.e. reduces the
    time resolution by 3.
    Similar to "Listen Attend Spell".
    https://arxiv.org/pdf/1508.01211.pdf
    """
    # [batch_size, max_time, num_units]
    batch_size, max_time, num_units = util.shape_list(inputs)

    if mask is not None:
        inputs *= tf.expand_dims(mask, -1)

    num_pad = tf.cast(tf.math.ceil(tf.divide(max_time, scale)) * scale, tf.int32) - max_time

    pads = [[0, 0], [0, num_pad], [0, 0]]
    inputs = tf.pad(inputs, pads)

    if mask is not None:
        pads = [[0, 0], [0, num_pad]]
        mask = tf.pad(mask, pads)

    concat_inputs = tf.reshape(inputs, (batch_size, -1, num_units * scale))
    if mask is not None:
        concat_mask = tf.reshape(mask, (batch_size, -1, scale))
        concat_mask = 1. - tf.cast(tf.less(tf.reduce_sum(concat_mask, -1), scale), tf.float32)

        return concat_inputs, concat_mask
    else:
        return concat_inputs


def encoder(source, params):
    hidden_size = params.hidden_size

    # extract logmel features
    source, mask, wavframes = speech.extract_logmel_features(source, params)
    target = source

    if params.use_nafm:
        x = wavframes
        with tf.compat.v1.variable_scope("feed_forward"):
            y = func.ffn_layer(
                x,
                params.filter_size,
                util.shape_list(wavframes)[-1],
                dropout=params.relu_dropout,
            )

            x = func.residual_fn(x, y, dropout=params.residual_dropout)
            x = func.layer_norm(x)

        with tf.compat.v1.variable_scope("feed_forward2"):
            y = func.ffn_layer(
                x,
                params.filter_size,
                util.shape_list(wavframes)[-1],
                dropout=params.relu_dropout,
            )

            x = func.residual_fn(x, y, dropout=params.residual_dropout)
            x = func.layer_norm(x)

        source = func.linear(x, util.shape_list(target)[-1], scope="pretrain")
        _source, _mask = source, mask

    # tried different settings for scale, turns out 3 is good
    source, mask = stacking(source, scale=3, mask=mask)
    if not params.sinusoid_posenc:
        source = source[:, :params.max_poslen]
        mask = mask[:, :params.max_poslen]
    source, mask = dtype.tf_to_float(source), dtype.tf_to_float(mask)

    # map from raw feature space to Transformer dimension
    inputs = func.linear(source, params.embed_size, scope="emb_mapper")

    # transformer is sensitive to the position encoding,
    if params.sinusoid_posenc:
        inputs = func.add_timing_signal(inputs)
    else:
        pos_emb = tf.compat.v1.get_variable("pos_embedding", [params.max_poslen, params.embed_size])

        ishp = util.shape_list(inputs)
        inputs += tf.expand_dims(pos_emb[:ishp[1]], 0)

    # this normalization layer deeply stabilize the gradient and optimization issue
    inputs = func.layer_norm(inputs)
    inputs = util.valid_apply_dropout(inputs, params.dropout)

    with tf.compat.v1.variable_scope("encoder"):
        x = inputs
        for layer in range(params.num_encoder_layer):
            if params.deep_transformer_init:
                layer_initializer = tf.variance_scaling_initializer(
                    params.initializer_gain * (layer + 1) ** -0.5,
                    mode="fan_avg",
                    distribution="uniform")
            else:
                layer_initializer = None
            with tf.compat.v1.variable_scope("layer_{}".format(layer), initializer=layer_initializer):
                with tf.compat.v1.variable_scope("self_attention"):
                    # suggest: encoder_localize-> pdp, decoder->none
                    y = func.dot_attention(
                        x,
                        None,
                        func.attention_bias(mask, "masking"),
                        hidden_size,
                        num_heads=params.num_heads,
                        dropout=params.attention_dropout,
                        localize=params.enc_localize,
                        pdp_r=params.pdp_r,
                    )

                    y = y['output']
                    x = func.residual_fn(x, y, dropout=params.residual_dropout)
                    x = func.layer_norm(x)

                with tf.compat.v1.variable_scope("feed_forward"):
                    y = func.ffn_layer(
                        x,
                        params.filter_size,
                        hidden_size,
                        dropout=params.relu_dropout,
                    )

                    x = func.residual_fn(x, y, dropout=params.residual_dropout)
                    x = func.layer_norm(x)

    source_encodes = x
    x_shp = util.shape_list(x)

    states = {
        "encodes": source_encodes,
        "decoder_initializer": {
            "layer_{}".format(l): {
                "k": dtype.tf_to_float(tf.zeros([x_shp[0], 0, hidden_size])),
                "v": dtype.tf_to_float(tf.zeros([x_shp[0], 0, hidden_size])),
            }
            for l in range(params.num_decoder_layer)
        },
        "mask": mask
    }

    if params.use_nafm:
        states['_target'] = target
        states['_source'] = _source
        states['_mask'] = _mask
    return states


def decoder(target, state, params, labels=None):
    mask = dtype.tf_to_float(tf.cast(target, tf.bool))
    hidden_size = params.hidden_size
    initializer = tf.random_normal_initializer(0.0, hidden_size ** -0.5)

    is_training = ('decoder' not in state)

    if is_training:
        target, mask = util.remove_invalid_seq(target, mask)

    embed_name = "embedding" if params.shared_source_target_embedding \
        else "tgt_embedding"
    tgt_emb = tf.compat.v1.get_variable(embed_name,
                              [params.tgt_vocab.size(), params.embed_size],
                              initializer=initializer)
    tgt_bias = tf.compat.v1.get_variable("bias", [params.embed_size])

    inputs = tf.gather(tgt_emb, target) * (hidden_size ** 0.5)
    inputs = tf.nn.bias_add(inputs, tgt_bias)

    # shift
    if is_training:
        inputs = tf.pad(inputs, [[0, 0], [1, 0], [0, 0]])
        inputs = inputs[:, :-1, :]
        inputs = func.add_timing_signal(inputs)
    else:
        inputs = tf.cond(tf.reduce_all(tf.equal(target, params.tgt_vocab.pad())),
                         lambda: tf.zeros_like(inputs),
                         lambda: inputs)
        mask = tf.ones_like(mask)
        inputs = func.add_timing_signal(inputs, time=dtype.tf_to_float(state['time']))

    inputs = util.valid_apply_dropout(inputs, params.dropout)

    with tf.compat.v1.variable_scope("decoder"):
        x = inputs
        for layer in range(params.num_decoder_layer):
            if params.deep_transformer_init:
                layer_initializer = tf.variance_scaling_initializer(
                    params.initializer_gain * (layer + 1) ** -0.5,
                    mode="fan_avg",
                    distribution="uniform")
            else:
                layer_initializer = None
            with tf.compat.v1.variable_scope("layer_{}".format(layer), initializer=layer_initializer):
                with tf.compat.v1.variable_scope("self_attention"):
                    y = func.dot_attention(
                        x,
                        None,
                        func.attention_bias(tf.shape(mask)[1], "causal"),
                        hidden_size,
                        num_heads=params.num_heads,
                        dropout=params.attention_dropout,
                        cache=None if is_training else state['decoder']['state']['layer_{}'.format(layer)],
                        localize=params.dec_localize,
                        pdp_r=params.pdp_r,
                        decode_step=None if is_training else state['time'],
                    )
                    if not is_training:
                        # k, v
                        state['decoder']['state']['layer_{}'.format(layer)] \
                            .update(y['cache'])

                    y = y['output']
                    x = func.residual_fn(x, y, dropout=params.residual_dropout)
                    x = func.layer_norm(x)

                with tf.compat.v1.variable_scope("cross_attention"):
                    y = func.dot_attention(
                        x,
                        state['encodes'],
                        func.attention_bias(state['mask'], "masking"),
                        hidden_size,
                        num_heads=params.num_heads,
                        dropout=params.attention_dropout,
                        cache=None if is_training else state['decoder']['state']['layer_{}'.format(layer)],
                        localize=params.encdec_localize,
                        pdp_r=params.pdp_r,
                    )
                    if not is_training:
                        # mk, mv
                        state['decoder']['state']['layer_{}'.format(layer)] \
                            .update(y['cache'])

                    y = y['output']
                    x = func.residual_fn(x, y, dropout=params.residual_dropout)
                    x = func.layer_norm(x)

                with tf.compat.v1.variable_scope("feed_forward"):
                    y = func.ffn_layer(
                        x,
                        params.filter_size,
                        hidden_size,
                        dropout=params.relu_dropout,
                    )

                    x = func.residual_fn(x, y, dropout=params.residual_dropout)
                    x = func.layer_norm(x)
    feature = x
    if 'dev_decode' in state:
        feature = x[:, -1, :]

    embed_name = "tgt_embedding" if params.shared_target_softmax_embedding \
        else "softmax_embedding"
    embed_name = "embedding" if params.shared_source_target_embedding \
        else embed_name
    softmax_emb = tf.compat.v1.get_variable(embed_name,
                                  [params.tgt_vocab.size(), params.embed_size],
                                  initializer=initializer)
    feature = tf.reshape(feature, [-1, params.embed_size])
    logits = tf.matmul(feature, softmax_emb, False, True)

    logits = tf.cast(logits, tf.float32)

    soft_label, normalizer = util.label_smooth(
        target,
        util.shape_list(logits)[-1],
        factor=params.label_smooth)
    centropy = tf.nn.softmax_cross_entropy_with_logits_v2(
        logits=logits,
        labels=soft_label
    )
    centropy -= normalizer
    centropy = tf.reshape(centropy, tf.shape(target))

    mask = tf.cast(mask, tf.float32)
    per_sample_loss = tf.reduce_sum(centropy * mask, -1) / tf.reduce_sum(mask, -1)
    loss = tf.reduce_mean(per_sample_loss)

    if is_training and params.ctc_enable:
        assert labels is not None

        # batch x seq x dim
        encoding = state['encodes']
        # CTC projection: adding one more symbol for blank
        ctc_label_size = params.src_vocab.size() + 1
        # Supporting CoLaCTC
        if params.cola_ctc_L > 0:
            ctc_label_size = params.cola_ctc_L + 1

        enc_logits = func.linear(encoding, ctc_label_size, scope="ctc_mapper")
        # seq dimension transpose
        enc_logits = tf.transpose(enc_logits, (1, 0, 2))

        enc_logits = tf.cast(enc_logits, tf.float32)

        with tf.name_scope('loss'):
            ctc_loss = tf.compat.v1.nn.ctc_loss(labels, enc_logits, tf.cast(tf.reduce_sum(state['mask'], -1), tf.int32),
                                      ignore_longer_outputs_than_inputs=True,
                                      preprocess_collapse_repeated=params.ctc_repeated)
            ctc_loss /= tf.reduce_sum(mask, -1)
            ctc_loss = tf.reduce_mean(ctc_loss)

        loss = params.ctc_alpha * ctc_loss + (1. - params.ctc_alpha) * loss

    if is_training and params.use_nafm:
        mse_loss = tf.reduce_sum((state["_source"] - state["target"]) ** 2, -1)
        per_sample_loss = tf.reduce_sum(mse_loss * state["_mask"], -1) / tf.reduce_sum(state["_mask"], -1)
        mse_loss = tf.reduce_mean(per_sample_loss)
        loss = loss + params.nafm_alpha * mse_loss

    # these mask tricks mainly used to deal with zero shapes, such as [0, 1]
    loss = tf.cond(tf.equal(tf.shape(target)[0], 0),
                   lambda: tf.constant(0, tf.float32),
                   lambda: loss)

    return loss, logits, state, per_sample_loss


def train_fn(features, params, initializer=None):
    with tf.compat.v1.variable_scope(params.scope_name or "model",
                           initializer=initializer,
                           reuse=tf.compat.v1.AUTO_REUSE,
                           dtype=tf.as_dtype(dtype.floatx()),
                           custom_getter=dtype.float32_variable_storage_getter):
        state = encoder(features['source'], params)
        loss, logits, state, _ = decoder(features['target'], state, params,
                                         labels=features['label'] if params.ctc_enable else None)

        return {
            "loss": loss
        }


def score_fn(features, params, initializer=None):
    params = copy.copy(params)
    params = util.closing_dropout(params)
    params.label_smooth = 0.0
    params.audio_dither=0.0
    with tf.compat.v1.variable_scope(params.scope_name or "model",
                           initializer=initializer,
                           reuse=tf.compat.v1.AUTO_REUSE,
                           dtype=tf.as_dtype(dtype.floatx()),
                           custom_getter=dtype.float32_variable_storage_getter):
        state = encoder(features['source'], params)
        _, _, _, scores = decoder(features['target'], state, params)

        return {
            "score": scores
        }


def infer_fn(params):
    params = copy.copy(params)
    params = util.closing_dropout(params)
    # NOTICE!@!!!
    params.audio_dither=0.0

    def encoding_fn(source):
        with tf.compat.v1.variable_scope(params.scope_name or "model",
                               reuse=tf.compat.v1.AUTO_REUSE,
                               dtype=tf.as_dtype(dtype.floatx()),
                               custom_getter=dtype.float32_variable_storage_getter):
            state = encoder(source, params)
            state["decoder"] = {
                "state": state["decoder_initializer"]
            }
            return state

    def decoding_fn(target, state, time):
        with tf.compat.v1.variable_scope(params.scope_name or "model",
                               reuse=tf.compat.v1.AUTO_REUSE,
                               dtype=tf.as_dtype(dtype.floatx()),
                               custom_getter=dtype.float32_variable_storage_getter):
            if params.search_mode == "cache":
                state['time'] = time
                step_loss, step_logits, step_state, _ = decoder(
                    target, state, params)
                del state['time']
            else:
                estate = encoder(state, params)
                estate['dev_decode'] = True
                _, step_logits, _, _ = decoder(target, estate, params)
                step_state = state

            return step_logits, step_state

    return encoding_fn, decoding_fn


# register the model, with a unique name
model.model_register("transformer", train_fn, score_fn, infer_fn)
