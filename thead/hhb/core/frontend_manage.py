# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
# pylint: disable=unused-argument, superfluous-parens, arguments-differ
# pylint: disable=import-outside-toplevel, no-else-return, inconsistent-return-statements
# pylint: disable=no-else-raise, broad-except
"""
Provides support to parse models from different frameworks into Relay IR.

Frontend classes do lazy-loading of modules on purpose, to reduce time spent on
loading the tool.
"""
import logging
import sys
from pathlib import Path

import tvm
from tvm import relay
from tvm.relay import expr as _expr
from tvm.driver.tvmc.frontends import Frontend
from tvm.driver.tvmc.frontends import KerasFrontend
from tvm.driver.tvmc.frontends import OnnxFrontend
from tvm.driver.tvmc.frontends import TensorflowFrontend
from tvm.driver.tvmc.frontends import TFLiteFrontend
from tvm.driver.tvmc.frontends import PyTorchFrontend

from .common import HHBException, convert_invalid_symbol


# pylint: disable=invalid-name
logger = logging.getLogger("HHB")


def remove_invalid_symbol(mod, params):
    class RemoveInvalidSymbol(relay.ExprMutator):
        """Remove invalid sysmbol from variable name in relay ir."""

        def visit_var(self, call):
            new_name_hint = convert_invalid_symbol(call.name_hint)
            return _expr.var(
                new_name_hint, shape=call.checked_type.shape, dtype=call.checked_type.dtype
            )

    mod["main"] = RemoveInvalidSymbol().visit(mod["main"])

    all_key = list(params.keys())
    for k in all_key:
        new_k = convert_invalid_symbol(k)
        params[new_k] = params.pop(k)

    return mod, params


class HHBKerasFrontend(KerasFrontend):
    """ Keras frontend for HHB. """

    def load(self, path, input_name=None, input_shape=None, output_name=None):
        keras_path = path[0]
        return super().load(keras_path)


class HHBOnnxFrontend(OnnxFrontend):
    """ ONNX frontend for HHB. """

    def load(self, path, input_name=None, input_shape=None, output_name=None):
        onnx_path = path[0]
        if (not input_name) and (not input_shape) and (not output_name):
            return super().load(onnx_path)
        import onnx

        onnx_model = onnx.load(onnx_path)
        if output_name:
            e = onnx.onnx.utils.Extractor(onnx_model)
            onnx_model = e.extract_model(input_name, output_name)
        input_dict = dict()
        for idx, name in enumerate(input_name):
            input_dict[name] = input_shape[idx]
        logger.info("Parse Onnx model and convert into Relay IR.")
        return relay.frontend.from_onnx(onnx_model, input_dict)


class HHBTensorflowFrontend(TensorflowFrontend):
    """ Tensorflow frontend for HHB. """

    def load(self, path, input_name=None, input_shape=None, output_name=None):
        pb_path = path[0]
        if (not input_name) and (not input_shape) and (not output_name):
            return super().load(pb_path)
        import tensorflow as tf

        try:
            tf_compat_v1 = tf.compat.v1
        except ImportError:
            tf_compat_v1 = tf
        import tvm.relay.testing.tf as tf_testing

        input_dict = dict()
        for idx, name in enumerate(input_name):
            input_dict[name] = input_shape[idx]

        with tf.io.gfile.GFile(pb_path, "rb") as tf_graph:
            content = tf_graph.read()

        # import tensorflow graph
        graph_def = tf_compat_v1.GraphDef()
        graph_def.ParseFromString(content)
        graph_def = tf_testing.ProcessGraphDefParam(graph_def)
        tf.import_graph_def(graph_def, name="")

        # with tf_compat_v1.Session() as sess:
        #     graph_def = tf_testing.AddShapesToGraphDef(sess, output_name)
        logger.info("Parse Tensorflow model and convert into Relay IR.")
        return relay.frontend.from_tensorflow(
            graph_def, layout="NCHW", shape=input_dict, outputs=output_name, input_layout="NCHW"
        )


class HHBTFLiteFrontend(TFLiteFrontend):
    """ TFLite frontend for HHB. """

    def load(self, path, input_name=None, input_shape=None, output_name=None):
        tflite_path = path[0]
        # pylint: disable=C0415
        import tflite.Model as model

        with open(tflite_path, "rb") as tf_graph:
            content = tf_graph.read()

        # tflite.Model.Model is tflite.Model in 1.14 and 2.1.0
        try:
            tflite_model = model.Model.GetRootAsModel(content, 0)
        except AttributeError:
            tflite_model = model.GetRootAsModel(content, 0)

        try:
            version = tflite_model.Version()
            logger.debug("tflite version %s", version)
        except Exception:
            raise TVMCException("input file not tflite")

        if version != 3:
            raise TVMCException("input file not tflite version 3")

        logger.debug("tflite_input_type")
        if input_shape is None:
            shape_dict, dtype_dict = TFLiteFrontend._input_type(tflite_model)
        else:
            shape_dict = {name: shape for name, shape in zip(input_name, input_shape)}
            dtype_dict = {name: "float32" for name in input_name}

        logger.debug("parse TFLite model and convert into Relay computation graph")
        target_layout = "NCHW"
        mod, params = relay.frontend.from_tflite_to_hhb(
            tflite_model, shape_dict, dtype_dict, target_layout, output_name
        )
        return mod, params


class HHBPyTorchFrontend(PyTorchFrontend):
    """ PyTorch frontend for HHB. """

    def load(self, path, input_name=None, input_shape=None, output_name=None):
        pytorch_path = path[0]
        return super().load(pytorch_path)


class HHBCaffeFrontend(Frontend):
    """ Caffe frontend for HHB. """

    @staticmethod
    def name():
        return "caffe"

    @staticmethod
    def suffixes():
        return ["prototxt", "caffemodel"]

    def _check_and_get_caffemodel(self, path):
        """ Check the imported model file whether satisfy the Caffe framework. """
        if isinstance(path, (list, tuple)) and len(path) == 2:
            from google.protobuf import text_format
            import tvm.relay.frontend.caffe_pb2 as pb

            prototxt_net = pb.NetParameter()
            caffemodel_net = pb.NetParameter()

            flag = True
            try:
                with open(path[0], "r") as f:
                    text_format.Merge(f.read(), prototxt_net)
                with open(path[1], "rb") as f:
                    caffemodel_net.ParseFromString(f.read())
            except Exception:
                flag = False

            if flag:
                return prototxt_net, caffemodel_net
            else:
                try:
                    with open(path[1], "r") as f:
                        text_format.Merge(f.read(), prototxt_net)
                    with open(path[0], "rb") as f:
                        caffemodel_net.ParseFromString(f.read())
                    return prototxt_net, caffemodel_net
                except Exception:
                    sys.stderr.write(
                        "Please input valid caffemodel file: .prototxt and .caffemodel\n"
                    )
                    sys.exit(-1)
        raise HHBException("Please input valid caffemodel file: .prototxt and .caffemodel\n")

    def load(self, path, input_name=None, input_shape=None, output_name=None):
        prototxt_net, caffemodel_net = self._check_and_get_caffemodel(path)

        shape_dict = {}
        dtype_dict = {}
        if input_name and input_shape:
            for idx, name in enumerate(input_name):
                shape_dict[name] = input_shape[idx]
                dtype_dict[name] = "float32"
        else:
            if len(prototxt_net.input) > 0:
                raise HHBException(
                    "Unsupported version of the model. Use "
                    "'upgrade_net_proto_text src.prototxt dst.prototxt' to upgrade the prototxt.\n"
                )
            else:
                for layer in prototxt_net.layer:
                    if layer.type == "Input":
                        iname = layer.top[0]
                        shape_dict[iname] = list(layer.input_param.shape[0].dim)
                        dtype_dict[iname] = "float32"
        logger.info("Parse Caffe model and convert into Relay IR.")
        mod, params, _ = relay.frontend.from_caffe(
            caffemodel_net, prototxt_net, shape_dict, dtype_dict
        )
        return mod, params


class HHBKaldiFrontend(Frontend):
    """ Kaldi frontend for HHB. """

    @staticmethod
    def name():
        return "kaldi"

    @staticmethod
    def suffixes():
        return ["txt"]

    def _check_and_get_kaldimodel(self, path):
        """ Check the imported model file whether satisfy the kaldi framework. """
        if isinstance(path, (list, tuple)) and len(path) == 1:
            from tvm.relay.frontend.kaldi_parse import KaldiLoader

            try:
                return relay.frontend.KaldiLoader(path[0]).load()
            except Exception:
                sys.stderr.write("Please input valid kaldi file: .txt\n")
                sys.exit(-1)
        raise HHBException("Please input valid kaldi file: .txt\n")

    def load(self, path, input_name=None, input_shape=None, output_name=None):
        kaldi_net = self._check_and_get_kaldimodel(path)

        shape_dict = {}
        dtype_dict = {}
        if input_name and input_shape:
            assert len(input_name) == 1, f"only need single input name, but get {input_name}"
            assert len(input_shape) == 1, f"only need single input shape, but get {input_shape}"
            shape_dict[input_name[0]] = input_shape[0]
            dtype_dict[input_name[0]] = "float32"
        else:
            layer = kaldi_net[0]
            if layer["token"] == "AffineTransform":
                iname = "input"
                shape_dict[iname] = list([1, layer["size_settings"][1]])
                dtype_dict[iname] = "float32"
            else:
                raise HHBException("Please input 'input_name' and 'input_shape'.")
        logger.debug("Parse Kaldi model and convert into Relay IR.")
        mod, params = relay.frontend.from_kaldi(kaldi_net, shape_dict, dtype_dict)
        return mod, params


ALL_HHB_FRONTENDS = [
    HHBKerasFrontend,
    HHBOnnxFrontend,
    HHBTensorflowFrontend,
    HHBTFLiteFrontend,
    HHBPyTorchFrontend,
    HHBCaffeFrontend,
    HHBKaldiFrontend,
]


def get_frontend_names():
    """Return the names of all supported frontends

    Returns
    -------
    list : list of str
        A list of frontend names as strings

    """
    return [frontend.name() for frontend in ALL_HHB_FRONTENDS]


def get_frontend_by_name(name):
    """
    This function will try to get a frontend instance, based
    on the name provided.

    Parameters
    ----------
    name : str
        the name of a given frontend

    Returns
    -------
    frontend : tvm.driver.tvmc.Frontend
        An instance of the frontend that matches with
        the file extension provided in `path`.

    """

    for frontend in ALL_HHB_FRONTENDS:
        if name == frontend.name():
            return frontend()

    raise HHBException(
        "unrecognized frontend '{0}'. Choose from: {1}\n".format(name, get_frontend_names())
    )


def guess_frontend(path):
    """
    This function will try to imply which framework is being used,
    based on the extension of the file provided in the path parameter.

    Parameters
    ----------
    path : list[str]
        The path to the model file.

    Returns
    -------
    frontend : tvm.driver.tvmc.Frontend
        An instance of the frontend that matches with
        the file extension provided in `path`.

    """
    if len(path) == 1:
        suffix = Path(path[0]).suffix.lower()
        if suffix.startswith("."):
            suffix = suffix[1:]

        for frontend in ALL_HHB_FRONTENDS:
            if suffix in frontend.suffixes():
                return frontend()
    else:
        suffix_list = []
        for p in path:
            suffix = Path(p).suffix.lower()
            if suffix.startswith("."):
                suffix_list.append(suffix[1:])
            else:
                raise HHBException(
                    "failed to infer the model format. Please specify --model-format\n"
                )
        for frontend in ALL_HHB_FRONTENDS:
            if len(suffix_list) == len(frontend.suffixes()):
                suffix_set = set(suffix_list)
                frontend_set = set(frontend.suffixes())
                if not (suffix_set - frontend_set):
                    return frontend()
        raise HHBException("failed to infer the model format. Please specify --model-format\n")


def import_model(path, model_format=None, input_name=None, input_shape=None, output_name=None):
    """Import a model from a supported framework into relay ir.

    Parameters
    ----------
    path : list[str]
        Path to a model file. There may be two files(.caffemodel, .prototxt) for Caffe model
    model_format : str, optional
        A string representing input model format
    input_name : list[str], optional
        The names of input node in the graph
    input_shape : list[list[int]], optional
        The shape of input node in the graph
    output_name : list[str], optional
        The name of output node in the graph

    Returns
    -------
    mod : tvm.IRModule
        The relay module for compilation
    params : dict of str to tvm.nd.NDArray
        The parameter dict to be used by relay
    """
    if model_format is not None:
        frontend = get_frontend_by_name(model_format)
    else:
        frontend = guess_frontend(path)
    if input_name and input_shape:
        assert len(input_name) == len(
            input_shape
        ), "The length of \
                input_name must be equal to that of input_shape."
    mod, params = frontend.load(path, input_name, input_shape, output_name)
    mod = relay.transform.InferType()(mod)
    mod, params = remove_invalid_symbol(mod, params)

    return mod, params
