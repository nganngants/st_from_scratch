# coding: utf-8

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math
import tensorflow as tf

from utils import util, dtype


def linear(x, dim, bias=True, ln=False,
           weight_initializer=None,
           bias_initializer=tf.zeros_initializer(),
           scope=None, custom_getter=None):
    """
    basic linear or feed forward layer
    :param x: input tensor or list
    :param dim: output dimension or list
    :param bias: whether use bias term
    :param ln: whether use layer normalization
    :param weight_initializer: you can set it if you want
    :param bias_initializer: you can set it if you want
    :param scope
    :return:
    """
    with tf.compat.v1.variable_scope(scope or "linear", values=[x],
                           dtype=tf.as_dtype(dtype.floatx()),
                           custom_getter=custom_getter):
        if not isinstance(x, (list, tuple)):
            x = [x]
        if not isinstance(dim, (list, tuple)):
            dim = [dim]

        if not ln:
            # by default, we concatenate inputs
            x = [tf.concat(x, -1)]

        outputs = []
        for oidx, osize in enumerate(dim):

            results = []
            for iidx, ix in enumerate(x):
                x_shp = util.shape_list(ix)
                xsize = x_shp[-1]

                W = tf.compat.v1.get_variable("W_{}_{}".format(oidx, iidx), [xsize, osize], initializer=weight_initializer)
                o = tf.matmul(tf.reshape(ix, [-1, xsize]), W)

                if ln:
                    o = layer_norm(o, scope="ln_{}_{}".format(oidx, iidx))
                results.append(o)

            o = tf.add_n(results)

            if bias:
                b = tf.compat.v1.get_variable("b_{}".format(oidx), [osize], initializer=bias_initializer)
                o = tf.nn.bias_add(o, b)
            x_shp = util.shape_list(x[0])[:-1]
            o = tf.reshape(o, tf.concat([x_shp, [osize]], 0))

            outputs.append(o)

        return outputs[0] if len(outputs) == 1 else outputs


def split_heads(inputs, num_heads, name=None):
    """ Split heads
    :param inputs: A tensor with shape [batch, length, channels]
    :param num_heads: An integer
    :param name: An optional string
    :returns: A tensor with shape [batch, heads, length, channels / heads]
    """

    with tf.name_scope(name or "split_heads"):
        x = inputs
        n = num_heads
        old_shape = x.get_shape().dims

        last = old_shape[-1]
        new_shape = old_shape[:-1] + [n] + [last // n if last else None]
        ret = tf.reshape(x, tf.concat([tf.shape(x)[:-1], [n, -1]], 0))
        ret.set_shape(new_shape)
        return tf.transpose(ret, [0, 2, 1, 3])


def combine_heads(inputs, name=None):
    """ Combine heads
    :param inputs: A tensor with shape [batch, heads, length, channels]
    :param name: An optional string
    :returns: A tensor with shape [batch, length, heads * channels]
    """

    with tf.name_scope(name or "combine_heads"):
        x = inputs
        x = tf.transpose(x, [0, 2, 1, 3])
        old_shape = x.get_shape().dims
        a, b = old_shape[-2:]
        new_shape = old_shape[:-2] + [a * b if a and b else None]
        x = tf.reshape(x, tf.concat([tf.shape(x)[:-2], [-1]], 0))
        x.set_shape(new_shape)

        return x


def dot_attention(query, memory, mem_mask, hidden_size,
                  ln=False, num_heads=1, cache=None, dropout=None,
                  pdp_r=16, out_map=True, scope=None,
                  decode_step=None, localize=None):
    """
    dotted attention model
    :param query: [batch_size, qey_len, dim]
    :param memory: [batch_size, seq_len, mem_dim] or None
    :param mem_mask: [batch_size, seq_len]
    :param hidden_size: attention space dimension
    :param ln: whether use layer normalization
    :param num_heads: attention head number
    :param dropout: attention dropout, default disable
    :param out_map: output additional mapping
    :param cache: cache-based decoding
    :param pdp_r: maximum position considered for pdp (parameterized distance penalty)
    :param decode_step: the time step of current decoding, 0-based
    :param localize: localization method for self-attention, including None, log, and pdp
    :param scope:
    :return: a value matrix, [batch_size, qey_len, mem_dim]
    """
    with tf.compat.v1.variable_scope(scope or "dot_attention", reuse=tf.compat.v1.AUTO_REUSE,
                           dtype=tf.as_dtype(dtype.floatx())):
        if memory is None:
            # suppose self-attention from queries alone
            h = linear(query, hidden_size * 3, ln=ln, scope="qkv_map")
            q, k, v = tf.split(h, 3, -1)

            if cache is not None:
                k = tf.concat([cache['k'], k], axis=1)
                v = tf.concat([cache['v'], v], axis=1)
                cache = {
                    'k': k,
                    'v': v,
                }
        else:
            q = linear(query, hidden_size, ln=ln, scope="q_map")
            if cache is not None and ('mk' in cache and 'mv' in cache):
                k, v = cache['mk'], cache['mv']
            else:
                k = linear(memory, hidden_size, ln=ln, scope="k_map")
                v = linear(memory, hidden_size, ln=ln, scope="v_map")

            if cache is not None:
                cache['mk'] = k
                cache['mv'] = v

        q = split_heads(q, num_heads)
        k = split_heads(k, num_heads)
        v = split_heads(v, num_heads)

        q *= (hidden_size // num_heads) ** (-0.5)

        q_shp = util.shape_list(q)
        k_shp = util.shape_list(k)

        q_len = q_shp[2] if decode_step is None else decode_step + 1
        r_lst = None if decode_step is None else 1

        # q * k => attention weights
        logits = tf.matmul(q, k, transpose_b=True)

        if mem_mask is not None:
            logits += mem_mask

        # consider localization
        if localize is not None and localize != "none":
            k_len = k_shp[2]

            q_rng = tf.range(q_len)
            k_rng = tf.range(k_len)

            # shape: len_Q x len_K
            dist = tf.expand_dims(q_rng, 1) - tf.expand_dims(k_rng, 0)

            if localize == "log":
                dist = tf.abs(dist) + 1
                log_dist = tf.math.log(tf.cast(dist, dtype=tf.float32))
                if r_lst is not None:
                    log_dist = log_dist[-r_lst:]
                logits -= tf.expand_dims(tf.expand_dims(log_dist, 0), 0)
            # implementation for the proposed parameterized penalty distance
            elif localize == "pdp":
                log_dist = tf.math.log(dtype.tf_to_float(tf.abs(dist) + 1))
                if r_lst is not None:
                    log_dist = log_dist[-r_lst:]

                # consider one more position for `zero`
                vocab_size = pdp_r + 1
                depth = num_heads

                # only consider absolute relative distance
                padding = vocab_size - 1
                mask = tf.cast(tf.less(tf.abs(dist), vocab_size), tf.int32)
                dist = mask * tf.abs(dist) + (1 - mask) * tf.ones_like(dist)*padding

                if r_lst is not None:
                    dist = dist[-r_lst:]

                pos_embedding = tf.compat.v1.get_variable("embeddings", [vocab_size, depth], initializer=tf.ones_initializer())
                # len_Q x len_K x num_heads
                dist_emb = tf.gather(pos_embedding, dist)
                dist_emb = tf.transpose(dist_emb, [2, 0, 1])
                logits += tf.expand_dims(dist_emb, 0) * (- tf.expand_dims(tf.expand_dims(log_dist, 0), 0))
            else:
                raise NotImplementedError("invalid localization function {}".format(localize))

        weights = tf.nn.softmax(logits)

        dweights = util.valid_apply_dropout(weights, dropout)

        # weights * v => attention vectors
        o = tf.matmul(dweights, v)

        o = combine_heads(o)

        if out_map:
            o = linear(o, hidden_size, ln=ln, scope="o_map")

        results = {
            'weights': weights,
            'output': o,
            'cache': cache
        }

        return results


def layer_norm(x, eps=None, scope=None, custom_getter=None):
    """Layer normalization layer"""
    if eps is None:
        eps = dtype.epsilon()
    with tf.compat.v1.variable_scope(scope or "layer_norm",
                           dtype=tf.as_dtype(dtype.floatx()),
                           custom_getter=custom_getter):
        layer_size = util.shape_list(x)[-1]

        scale = tf.compat.v1.get_variable("scale", [layer_size], initializer=tf.ones_initializer())
        offset = tf.compat.v1.get_variable("offset", [layer_size], initializer=tf.zeros_initializer())

        mean = tf.reduce_mean(x, -1, keep_dims=True)
        var = tf.reduce_mean((x - mean) ** 2, -1, keep_dims=True)

        return scale * (x - mean) * tf.math.rsqrt(var + eps) + offset


def rms_norm(x, eps=None, scope=None):
    """RMS-based Layer normalization layer"""
    if eps is None:
        eps = dtype.epsilon()
    with tf.compat.v1.variable_scope(scope or "rms_norm",
                           dtype=tf.as_dtype(dtype.floatx())):
        layer_size = util.shape_list(x)[-1]

        scale = tf.compat.v1.get_variable("scale", [layer_size], initializer=tf.ones_initializer())

        ms = tf.reduce_mean(x ** 2, -1, keep_dims=True)

        return scale * x * tf.math.rsqrt(ms + eps)


def residual_fn(x, y, dropout=None):
    """Residual Connection"""
    y = util.valid_apply_dropout(y, dropout)
    return x + y


def ffn_layer(x, d, d_o, dropout=None, scope=None):
    """FFN layer in Transformer"""
    with tf.compat.v1.variable_scope(scope or "ffn_layer",
                           dtype=tf.as_dtype(dtype.floatx())):
        hidden = linear(x, d, scope="enlarge")
        hidden = tf.nn.relu(hidden)

        hidden = util.valid_apply_dropout(hidden, dropout)

        output = linear(hidden, d_o, scope="output")

        return output


def add_timing_signal(x, min_timescale=1.0, max_timescale=1.0e4,
                      time=None, name=None):
    """Transformer Positional Embedding"""

    with tf.name_scope(name, default_name="add_timing_signal", values=[x]):
        length = tf.shape(x)[1]
        channels = tf.shape(x)[2]
        if time is None:
            position = dtype.tf_to_float(tf.range(length))
        else:
            # decoding position embedding
            position = tf.expand_dims(time, 0)
        num_timescales = channels // 2

        log_timescale_increment = (
                math.log(float(max_timescale) / float(min_timescale)) /
                (dtype.tf_to_float(num_timescales) - 1)
        )
        inv_timescales = min_timescale * tf.exp(
            dtype.tf_to_float(tf.range(num_timescales)) * -log_timescale_increment
        )

        scaled_time = (tf.expand_dims(position, 1) *
                       tf.expand_dims(inv_timescales, 0))
        signal = tf.concat([tf.sin(scaled_time), tf.cos(scaled_time)], axis=1)
        signal = tf.pad(signal, [[0, 0], [0, tf.math.mod(channels, 2)]])
        signal = tf.reshape(signal, [1, length, channels])

        return x + signal


def attention_bias(inputs, mode, inf=None, name=None):
    """ A bias tensor used in attention mechanism"""

    if inf is None:
        inf = - dtype.inf()

    with tf.name_scope(name, default_name="attention_bias", values=[inputs]):
        if mode == "causal":
            length = inputs
            lower_triangle = tf.linalg.band_part(
                tf.ones([length, length]), -1, 0
            )
            ret = dtype.tf_to_float(inf * (1.0 - lower_triangle))
            return tf.reshape(ret, [1, 1, length, length])
        elif mode == "masking":
            mask = inputs
            ret = (1.0 - mask) * inf
            return tf.expand_dims(tf.expand_dims(ret, 1), 1)
        elif mode == "aan":
            length = tf.shape(inputs)[1]
            diagonal = tf.eye(length)
            cum_factor = tf.expand_dims(tf.cumsum(diagonal, axis=0), 0)
            mask = tf.expand_dims(inputs, 1) * tf.expand_dims(inputs, 2)
            mask *= dtype.tf_to_float(cum_factor)
            weight = tf.nn.softmax(mask + (1.0 - mask) * inf)
            weight *= mask
            return weight
        else:
            raise ValueError("Unknown mode %s" % mode)
