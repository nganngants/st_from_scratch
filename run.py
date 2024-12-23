# coding: utf-8

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import time
import os
import random
import socket

import numpy as np
import tensorflow as tf
import tensorflow.contrib as tc

import models
import main as graph
from vocab import Vocab
from utils.recorder import Recorder
from utils import dtype, util


logger = tf.get_logger()
logger.propagate = False


# define global initial parameters
global_params = tc.training.HParams(
    # whether share source and target word embedding
    shared_source_target_embedding=False,
    # whether share target and softmax word embedding
    shared_target_softmax_embedding=True,

    # decoding maximum length: source length + decode_length
    decode_length=50,
    # beam size
    beam_size=4,
    # length penalty during beam search
    decode_alpha=0.6,
    decode_beta=1./6.,
    # noise beam search with gumbel
    enable_noise_beam_search=False,
    # beam search temperature, sharp or flat prediction
    beam_search_temperature=1.0,
    # return top elements, not used
    top_beams=1,
    # which version of beam search to use
    # cache or dev
    search_mode="cache",

    # distance considered for PDP
    pdp_r=512,

    # speech feature number
    # not that meaningful, we extracted mel features of dimension 40
    #   after applying deltas, the feature grows to 120
    audio_sample_rate=16000,
    audio_preemphasis=0.97,
    # note, disable it after training
    audio_dither=1.0 / np.iinfo(np.int16).max,
    audio_frame_length=25.0,
    audio_frame_step=10.0,
    audio_lower_edge_hertz=20.0,
    audio_upper_edge_hertz=8000.0,
    audio_num_mel_bins=80,
    audio_add_delta_deltas=True,

    # ASR pretrained model path
    asr_pretrain="",
    # whether filter variables from ASR initialization, such as not initlaize global steps
    filter_variables=False,

    # lrate decay
    # number of shards
    nstable=4,
    # warmup steps: start point for learning rate stop increaing
    warmup_steps=4000,
    # select strategy: noam, gnmt+, epoch, score and vanilla
    lrate_strategy="noam",
    # learning decay rate
    lrate_decay=0.5,
    # cosine learning rate schedule period
    cosine_period=5000,
    # cosine factor
    cosine_factor=1,

    # early stopping
    estop_patience=100,

    # initialization
    # type of initializer
    initializer="uniform",
    # initializer range control
    initializer_gain=0.08,

    # parameters for rnnsearch
    # encoder and decoder hidden size
    hidden_size=1000,
    # source and target embedding size
    embed_size=620,
    # dropout value
    dropout=0.1,
    relu_dropout=0.1,
    residual_dropout=0.1,
    # label smoothing value
    label_smooth=0.1,
    # model name
    model_name="transformer",
    # scope name
    scope_name="transformer",
    # filter size for transformer
    filter_size=2048,
    # attention dropout
    attention_dropout=0.1,
    # the number of encoder layers, valid for deep nmt
    num_encoder_layer=6,
    # the number of decoder layers, valid for deep nmt
    num_decoder_layer=6,
    # the number of attention heads
    num_heads=8,

    # sample rate * N / 100
    max_frame_len=100,
    max_text_len=100,
    # constant batch size at 'batch' mode for batch-based batching
    batch_size=80,
    # constant token size at 'token' mode for token-based batching
    token_size=3000,
    # token or batch-based data iterator
    batch_or_token='token',
    # batch size for decoding, i.e. number of source sentences decoded at the same time
    eval_batch_size=32,
    # whether shuffle batches during training
    shuffle_batch=True,
    # data leak buffer threshold
    data_leak_ratio=0.5,

    # whether use multiprocessing deal with data reading, default true
    process_num=1,
    # buffer size controls the number of sentences readed in one time,
    buffer_size=100,
    # a unique queue in multi-thread reading process
    input_queue_size=100,
    output_queue_size=100,

    # source vocabulary
    src_vocab_file="",
    # target vocabulary
    tgt_vocab_file="",
    # source train file
    src_train_path="",
    src_train_file="",
    # target train file
    tgt_train_file="",
    # ctc train file
    ctc_train_file="",
    # source development file
    src_dev_path="",
    src_dev_file="",
    # target development file
    tgt_dev_file="",
    # source test file
    src_test_path="",
    src_test_file="",
    # target test file
    tgt_test_file="",
    # output directory
    output_dir="",
    # output during testing
    test_output="",

    # adam optimizer hyperparameters
    beta1=0.9,
    beta2=0.999,
    epsilon=1e-9,
    # gradient clipping value
    clip_grad_norm=5.0,
    # the gradient norm upper bound, to avoid wired large gradient norm, only works for safe nan mode
    gnorm_upper_bound=1e20,
    # initial learning rate
    lrate=1e-5,
    # minimum learning rate
    min_lrate=0.0,
    # maximum learning rate
    max_lrate=1.0,

    # maximum epochs
    epoches=10,
    # the effective batch size is: batch/token size * update_cycle * num_gpus
    # sequential update cycle
    update_cycle=1,
    # the number of gpus
    gpus=[0],

    # enable safely handle nan
    safe_nan=False,
    # exponential moving average for stability, disabled by default
    ema_decay=-1.,

    # enable training deep transformer
    deep_transformer_init=False,

    # print information every disp_freq training steps
    disp_freq=100,
    # evaluate on the development file every eval_freq steps
    eval_freq=10000,
    # save the model parameters every save_freq steps
    save_freq=5000,
    # print sample translations every sample_freq steps
    sample_freq=1000,
    # saved checkpoint number
    checkpoints=5,
    best_checkpoints=1,
    # the maximum training steps, program with stop if epochs or max_training_steps is meet
    max_training_steps=1000,

    # random control, not so well for tensorflow.
    random_seed=1234,
    # whether or not train from checkpoint
    train_continue=True,

    # provide interface to modify the default datatype
    default_dtype="float32",
    dtype_epsilon=1e-8,
    dtype_inf=1e8,
    loss_scale=1.0,

    # speech-specific settings
    sinusoid_posenc=True,
    max_poslen=2048,
    ctc_repeated=False,
    ctc_enable=False,
    ctc_alpha=0.3,      # ctc loss factor
    enc_localize="log",
    dec_localize="none",
    encdec_localize="none",

    # cola ctc settings
    # -1: disable cola ctc, in our paper we set 256.
    cola_ctc_L=-1,

    # neural acoustic feature modeling
    use_nafm=False,
    nafm_alpha=0.05,

)

flags = tf.flags
flags.DEFINE_string("config", "", "Additional Mergable Parameters")
flags.DEFINE_string("parameters", "", "Command Line Refinable Parameters")
flags.DEFINE_string("name", "model", "Description of the training process for distinguishing")
flags.DEFINE_string("mode", "train", "train or test or ensemble")


# saving model configuration
def save_parameters(params, output_dir):
    if not tf.io.gfile.exists(output_dir):
        tf.io.gfile.mkdir(output_dir)

    param_name = os.path.join(output_dir, "param.json")
    with tf.io.gfile.GFile(param_name, "w") as writer:
        print("Saving parameters into {}"
                        .format(param_name))
        writer.write(params.to_json())


# load model configuration
def load_parameters(params, output_dir):
    param_name = os.path.join(output_dir, "param.json")
    param_name = os.path.abspath(param_name)

    if tf.io.gfile.exists(param_name):
        print("Loading parameters from {}"
                        .format(param_name))
        with tf.io.gfile.GFile(param_name, 'r') as reader:
            json_str = reader.readline()
            params.parse_json(json_str)
    return params


# build training process recorder
def setup_recorder(params):
    recorder = Recorder()
    # This is for early stopping, currently I did not use it
    recorder.bad_counter = 0    # start from 0
    recorder.estop = False

    recorder.lidx = -1      # local data index
    recorder.step = 0       # global step, start from 0
    recorder.epoch = 1      # epoch number, start from 1
    recorder.lrate = params.lrate     # running learning rate
    recorder.history_scores = []
    recorder.valid_script_scores = []

    # trying to load saved recorder
    record_path = os.path.join(params.output_dir, "record.json")
    record_path = os.path.abspath(record_path)
    if tf.io.gfile.exists(record_path):
        recorder.load_from_json(record_path)

    params.add_hparam('recorder', recorder)
    return params


# print model configuration
def print_parameters(params):
    print("The Used Configuration:")
    for k, v in params.values().items():
        print("%s\t%s", k.ljust(20), str(v).ljust(20))
    print("")


def main(_):
    # set up logger
    tf.compat.v1.logging.set_verbosity(tf.compat.v1.logging.INFO)

    print("Welcome Using Zero :)")

    pid = os.getpid()
    print("Your pid is {0} and use the following command to force kill your running:\n"
                    "'pkill -9 -P {0}; kill -9 {0}'".format(pid))
    # On clusters, this could tell which machine you are running
    print("Your running machine name is {}".format(socket.gethostname()))

    # load registered models
    util.dynamic_load_module(models, prefix="models")

    params = global_params

    # try loading parameters
    # priority: command line > saver > default
    params.parse(flags.FLAGS.parameters)
    if os.path.exists(flags.FLAGS.config):
        params.override_from_dict(eval(open(flags.FLAGS.config).read()))
    params = load_parameters(params, params.output_dir)
    # override
    if os.path.exists(flags.FLAGS.config):
        params.override_from_dict(eval(open(flags.FLAGS.config).read()))
    params.parse(flags.FLAGS.parameters)

    # set up random seed
    random.seed(params.random_seed)
    np.random.seed(params.random_seed)
    tf.compat.v1.set_random_seed(params.random_seed)

    # loading vocabulary
    print("Begin Loading Vocabulary")
    start_time = time.time()
    params.src_vocab = Vocab(params.src_vocab_file)
    params.tgt_vocab = Vocab(params.tgt_vocab_file)
    print("End Loading Vocabulary, Source Vocab Size {}, "
                    "Target Vocab Size {}, within {} seconds"
                    .format(params.src_vocab.size(), params.tgt_vocab.size(),
                            time.time() - start_time))

    # print parameters
    print_parameters(params)

    # set up the default datatype
    dtype.set_floatx(params.default_dtype)
    dtype.set_epsilon(params.dtype_epsilon)
    dtype.set_inf(params.dtype_inf)

    mode = flags.FLAGS.mode
    if mode == "train":
        # save parameters
        save_parameters(params, params.output_dir)

        # load the recorder
        params = setup_recorder(params)

        graph.train(params)
    elif mode == "test":
        graph.evaluate(params)
    elif mode == "score":
        graph.scorer(params)
    else:
        tf.logging.error("Invalid mode: {}".format(mode))


if __name__ == '__main__':
    print(f"Tensorflow version: {tf.__version__}")
    tf.compat.v1.app.run()
