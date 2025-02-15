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
# pylint: disable=invalid-name, unused-argument, missing-docstring, unused-import
# pylint: disable=import-outside-toplevel
"""Custom fusion pass."""
import logging
import numpy as np
from tvm import relay
from ..op.strategy.generic import is_depthwise_conv2d
from ..frontend.common import infer_shape
from ..expr_functor import ExprMutator
from ..expr import Var, Call, TupleGetItem, Constant, Tuple, const
from .. import function
from ..transform import function_pass
from ._convert_to_csi import _qnn_attrs

logger = logging.getLogger("HHB")


def split_relu(call, op_args):
    concat_attr = _qnn_attrs(op_args[0].attrs)
    relu_attr = _qnn_attrs(call.attrs)
    base_name = relu_attr["layer_name"]
    new_tuple_args = []
    for i, pre_call in enumerate(op_args[0].args[0]):
        relu_attr["q_params"][0] = relu_attr["q_params"][1] = concat_attr["q_params"][i]
        relu_attr["layer_name"] = base_name + f"_{i}" if base_name else base_name
        new_relu = relay.qnn.op.csi_relu(pre_call, **relu_attr)
        new_tuple_args.append(new_relu)
    concat_attr["layer_name"] = base_name + "_concat" if base_name else base_name
    new_concat = relay.qnn.op.csi_concatenate(Tuple(new_tuple_args), **concat_attr)
    return new_concat


def _split_q_params(q_params, num_group, group_size, left_group, keep_input=True):
    flags = q_params[:3]
    quant_params = q_params[3:]
    q_len = len(quant_params)
    if q_len == 2:
        if keep_input:
            return [q_params] * (num_group + 1)
        return [q_params] * num_group

    num_list = [[i * group_size * 2, (i + 1) * group_size * 2] for i in range(num_group)]
    if left_group:
        num_list[-1] = [q_len - left_group * 2, q_len]
    out = [flags + quant_params[start:end] for start, end in num_list]
    if keep_input:
        return [q_params] + out
    return out


def split_group(in_data, weight, bias, attr, max_groups):
    assert attr["data_layout"] == "NCHW", Exception("Only support NCHW layout.")

    if max_groups >= attr["groups"]:
        return relay.qnn.op.csi_conv2d(in_data, const(weight), const(bias), **attr)

    in_shape = infer_shape(in_data)
    min_in_group = in_shape[1] // attr["groups"]
    in_group_size = min_in_group * max_groups

    w_shape = weight.shape
    min_w_group = w_shape[0] // attr["groups"]
    w_group_size = min_w_group * max_groups
    num_group = np.floor_divide(in_shape[1], in_group_size).astype(int)
    left_group = np.floor_divide(in_shape[1] - num_group * in_group_size, min_in_group).astype(int)

    num_w_group = np.floor_divide(weight.shape[0], w_group_size).astype(int)
    left_w_group = np.floor_divide(
        weight.shape[0] - num_w_group * w_group_size, min_w_group
    ).astype(int)
    assert num_group == num_w_group
    assert left_group == left_w_group

    # split in_data
    if left_group:
        num_group += 1
    index = [i * max_groups * min_in_group for i in range(num_group)][1:]

    base_name = attr["layer_name"]
    split_attr = {"axis": 1}
    split_attr["q_params"] = _split_q_params(
        attr["q_params"][0], num_group, in_group_size, left_group, True
    )
    split_attr["out_dtype"] = attr["out_dtype"]
    split_attr["layer_name"] = base_name + "_split"
    input_sp = relay.qnn.op.csi_split(in_data, index, **split_attr)
    input_sp = relay.TupleWrapper(input_sp, num_group)

    # split weight
    concat_tuple = []
    split_params = []
    output_q = attr["q_params"][3]
    weight_q_params = _split_q_params(
        attr["q_params"][1], num_group, w_group_size, left_group, False
    )
    bias_q_params = _split_q_params(attr["q_params"][2], num_group, w_group_size, left_group, False)
    out_q_params = _split_q_params(attr["q_params"][3], num_group, w_group_size, left_group, False)
    for i in range(num_group):
        s_input = input_sp[i]
        attr["q_params"][0] = split_attr["q_params"][i + 1]
        attr["q_params"][1] = weight_q_params[i]
        attr["q_params"][2] = bias_q_params[i]
        attr["q_params"][3] = out_q_params[i]
        if i == num_group - 1 and left_group:
            attr["groups"] = left_group
            attr["channels"] = left_w_group * min_w_group
            s_weight = weight[-left_w_group * min_w_group :, :, :, :]
            s_b = bias[-left_w_group * min_w_group :] if bias.size > 1 else bias
        else:
            attr["groups"] = max_groups
            attr["channels"] = w_group_size
            s_weight = weight[i * w_group_size : (i + 1) * w_group_size, :, :, :]
            s_b = bias[i * w_group_size : (i + 1) * w_group_size] if bias.size > 1 else bias

        attr["layer_name"] = base_name + f"_{i}" if base_name else base_name
        s_w_expr = const(s_weight)
        s_b_expr = const(s_b)
        if logging.DEBUG >= logger.getEffectiveLevel():
            split_params.append(
                {
                    "groups": attr["groups"],
                    "s_input": infer_shape(s_input),
                    "s_w_shape": s_weight.shape,
                }
            )
        s_conv = relay.qnn.op.csi_conv2d(s_input, s_w_expr, s_b_expr, **attr)
        concat_tuple.append(s_conv)

    logger.debug("splitted conv %s %s by parmas: %s", base_name, in_shape, split_params)
    concat_name = base_name + "_concat" if base_name else base_name
    concat_q_params = out_q_params + [output_q]
    return relay.qnn.op.csi_concatenate(concat_tuple, 1, concat_q_params, concat_name)


class MaxGroupSpliter(ExprMutator):
    """Split depthwise by group"""

    def __init__(self, max_groups=10):
        super(MaxGroupSpliter, self).__init__()
        self.max_groups = max_groups

    def visit_call(self, call):
        op_args = [self.visit(arg) for arg in call.args]
        if call.op.name == "qnn.csi.conv2d":
            conv_attrs = _qnn_attrs(call.attrs)
            # if not depthwise conv
            if (
                conv_attrs["groups"] <= self.max_groups
                or conv_attrs["groups"] != conv_attrs["channels"]
            ):
                return Call(call.op, op_args, call.attrs)
            in_data = op_args[0]
            weight = op_args[1].data.asnumpy()
            bias = op_args[2].data.asnumpy()
            new_call = split_group(in_data, weight, bias, conv_attrs, self.max_groups)
            return new_call

        if call.op.name == "qnn.csi.relu":
            if isinstance(op_args[0], Call) and op_args[0].op.name == "qnn.csi.concatenate":
                return split_relu(call, op_args)

        new_call = Call(call.op, op_args, call.attrs)
        return new_call

    def visit_function(self, fn):
        new_params = [self.visit(x) for x in fn.params]
        new_body = self.visit(fn.body)
        return function.Function(list(new_params), new_body)


def split_channel(in_data, weight, bias, attr, max_out_channel):
    """Split output channel by threshold"""
    assert attr["data_layout"] == "NCHW", Exception("Only support NCHW layout.")

    w_shape = weight.shape
    if max_out_channel >= w_shape[0]:
        return relay.qnn.op.csi_conv2d(in_data, const(weight), const(bias), **attr)

    num_out = np.floor_divide(w_shape[0], max_out_channel).astype(int)
    left_out = w_shape[0] - num_out * max_out_channel
    if left_out:
        num_out += 1
    base_name = attr["layer_name"]

    # split weight
    concat_tuple = []
    split_params = []
    output_q = attr["q_params"][3]
    weight_q_params = _split_q_params(
        attr["q_params"][1], num_out, max_out_channel, left_out, False
    )
    bias_q_params = _split_q_params(attr["q_params"][2], num_out, max_out_channel, left_out, False)
    out_q_params = _split_q_params(attr["q_params"][3], num_out, max_out_channel, left_out, False)
    for i in range(num_out):
        attr["q_params"][1] = weight_q_params[i]
        attr["q_params"][2] = bias_q_params[i]
        attr["q_params"][3] = out_q_params[i]
        if i == num_out - 1 and left_out:
            attr["channels"] = left_out
            s_weight = weight[-left_out:, :, :, :]
            s_b = bias[-left_out:] if bias.size > 1 else bias
        else:
            attr["channels"] = max_out_channel
            s_weight = weight[i * max_out_channel : (i + 1) * max_out_channel, :, :, :]
            s_b = bias[i * max_out_channel : (i + 1) * max_out_channel] if bias.size > 1 else bias

        attr["layer_name"] = base_name + f"_{i}" if base_name else base_name
        s_w_expr = const(s_weight)
        s_b_expr = const(s_b)
        if logging.DEBUG >= logger.getEffectiveLevel():
            split_params.append(
                {
                    "channels": attr["channels"],
                    "in_shape": infer_shape(in_data),
                    "w_shape": s_weight.shape,
                }
            )
        s_conv = relay.qnn.op.csi_conv2d(in_data, s_w_expr, s_b_expr, **attr)
        concat_tuple.append(s_conv)

    logger.debug("splitted conv %s %s by params: %s", base_name, infer_shape(in_data), split_params)
    concat_name = base_name + "_concat" if base_name else base_name
    concat_q_params = out_q_params + [output_q]
    return relay.qnn.op.csi_concatenate(concat_tuple, 1, concat_q_params, concat_name)


class OutChannelSpliter(ExprMutator):
    """Split common convolution by out_channel"""

    def __init__(self, max_out_channel=32):
        super(OutChannelSpliter, self).__init__()
        self.max_out_channel = max_out_channel

    def visit_call(self, call):
        op_args = [self.visit(arg) for arg in call.args]
        if call.op.name == "qnn.csi.conv2d":
            in_data = op_args[0]
            weight = op_args[1].data.asnumpy()
            bias = op_args[2].data.asnumpy()
            conv_attrs = _qnn_attrs(call.attrs)
            # for depthwise convolution and group convolution
            if conv_attrs["groups"] != 1 or weight.shape[0] <= self.max_out_channel:
                return Call(call.op, op_args, call.attrs)
            # for common convolution
            new_call = split_channel(in_data, weight, bias, conv_attrs, self.max_out_channel)
            return new_call

        if call.op.name == "qnn.csi.relu":
            if isinstance(op_args[0], Call) and op_args[0].op.name == "qnn.csi.concatenate":
                return split_relu(call, op_args)

        new_call = Call(call.op, op_args, call.attrs)
        return new_call

    def visit_function(self, fn):
        new_params = [self.visit(x) for x in fn.params]
        new_body = self.visit(fn.body)
        return function.Function(list(new_params), new_body)


def split_kernel_size(in_data, weight, bias, attr, max_kernel_size, weight_byte):
    """Split filter by filter size"""
    assert attr["data_layout"] == "NCHW", Exception("Only support NCHW layout.")

    single_size = weight[0].size * weight_byte
    if single_size > max_kernel_size:
        raise Exception("max_kernel_size must larger than single_kernel_size")
    max_out_channel = np.floor_divide(max_kernel_size, single_size).astype(int)

    if max_out_channel >= weight.shape[0]:
        return relay.qnn.op.csi_conv2d(in_data, const(weight), const(bias), **attr)

    return split_channel(in_data, weight, bias, attr, max_out_channel)


class KernelSizeSpliter(ExprMutator):
    """Split common convolution by filter size"""

    def __init__(self, config, max_kernel_size=None):
        super(KernelSizeSpliter, self).__init__()
        self.max_kernel_size = max_kernel_size
        self.weight_byte = config.nbit_weight // 8

    def visit_call(self, call):
        op_args = [self.visit(arg) for arg in call.args]
        if call.op.name == "qnn.csi.conv2d":
            in_data = op_args[0]
            weight = op_args[1].data.asnumpy()
            bias = op_args[2].data.asnumpy()
            conv_attrs = _qnn_attrs(call.attrs)
            # for depthwise convolution and group convolution
            if conv_attrs["groups"] != 1 or weight.size * self.weight_byte <= self.max_kernel_size:
                return Call(call.op, op_args, call.attrs)
            # for common convolution
            new_call = split_kernel_size(
                in_data, weight, bias, conv_attrs, self.max_kernel_size, self.weight_byte
            )
            return new_call

        if call.op.name == "qnn.csi.relu":
            if isinstance(op_args[0], Call) and op_args[0].op.name == "qnn.csi.concatenate":
                return split_relu(call, op_args)

        return Call(call.op, op_args, call.attrs)

    def visit_function(self, fn):
        new_params = [self.visit(x) for x in fn.params]
        new_body = self.visit(fn.body)
        return function.Function(list(new_params), new_body)


def _infer_shape(in_shape, w_shape, padding, dilation, strides, group=1):
    oshape = [in_shape[0], w_shape[0] // group, 0, 0]
    pad_top, pad_left, pad_down, pad_right = padding
    dilated_ksize_y = 1 + (w_shape[2] - 1) * dilation[0]
    dilated_ksize_x = 1 + (w_shape[3] - 1) * dilation[1]

    oshape[2] = (
        np.floor_divide(in_shape[2] + pad_top + pad_down - dilated_ksize_y, strides[0]).astype(int)
        + 1
    )
    oshape[3] = (
        np.floor_divide(in_shape[3] + pad_left + pad_right - dilated_ksize_x, strides[1]).astype(
            int
        )
        + 1
    )

    return oshape


def get_max_row(
    sram_size,
    in_shape,
    w_shape,
    s_padding,
    dilation,
    strides,
    contain_weight,
    activation_byte,
    weight_byte,
):
    max_row = w_shape[2]
    while True:
        max_row += 1
        s_in_shape = [in_shape[0], in_shape[1], max_row, in_shape[3]]
        s_in_size = np.prod(s_in_shape) * activation_byte

        s_out_shape = _infer_shape(s_in_shape, w_shape, s_padding, dilation, strides)
        s_out_size = np.prod(s_out_shape) * activation_byte
        left_size = sram_size - (s_in_size + s_out_size)

        if contain_weight:
            left_size -= np.prod(w_shape) * weight_byte

        if left_size < 0:
            max_row -= 1
            if max_row <= 0:
                raise Exception("value error!")
            return max_row


def get_end(orig_end, end_list):
    floor_data = end_list[0]
    for x in end_list:
        if orig_end >= x:
            floor_data = x
        else:
            return floor_data
    return end_list[-1]


def get_start(orig_start, start_list, end_list):
    for s, e in zip(start_list, end_list):
        if s == orig_start:
            return s
        if s < orig_start <= e:
            return s
        if s == e:
            if orig_start in start_list:
                return orig_start
            return get_start(orig_start + 1, start_list, end_list)
    for s in start_list:
        if s > orig_start:
            return s
    raise Exception("start index find error!")


def split_input_by_max_row(
    in_data, in_shape, weight, bias, attrs, max_row, max_out_channel=None, max_groups=None
):
    w_shape = weight.shape
    d_h = in_shape[2]
    k_h = w_shape[2]
    if max_row < k_h:
        raise Exception("max row must larger than kernel's height size.")

    padding = attrs["padding"]
    strides = attrs["strides"]
    dilation = attrs["dilation"]
    groups = attrs["groups"]

    _, _, o_h, _ = _infer_shape(in_shape, w_shape, padding, dilation, strides, groups)

    # compute [start, end] number for each row grid
    start_list = [-padding[0] + i * strides[1] for i in range(o_h)]
    end_list = [i + k_h - 1 for i in start_list]

    row_index = []
    start_index = 0
    end_index = 0
    count_flag = 0
    while end_index < d_h:
        end_index = start_index + max_row - 1
        end_index = get_end(end_index, end_list)
        if end_index > end_list[-1]:
            raise Exception("error end index.")
        row_index.append([start_index, end_index])
        if len(row_index) == o_h or end_index == end_list[-1]:
            break
        if end_index >= d_h - 1:
            row_index[-1][1] = d_h - 1
            break
        start_index = get_start(end_index + 1, start_list, end_list)
        count_flag += 1
        if count_flag > o_h:
            raise Exception("loop error.")

    # split in_data
    concat_tuple = []
    base_name = attrs["layer_name"]
    base_group = attrs["groups"]
    logger.debug("original conv shape: input=%s s_w_shape=%s", infer_shape(in_data), weight.shape)
    split_params = []
    for i, (start_row, end_row) in enumerate(row_index):
        # np.mgrid can get every element
        # indices = np.mgrid[0:in_shape[0], 0:in_shape[1], s:e+1]
        # s_input = relay.gather_nd(data, indices_expr)
        # take can get all elements along axis by set start and end
        # if i == len(row_index) - 1:
        #     if end_row != d_h -1 and d_h - start_row <= max_row:
        #         end_row = d_h -1
        indices = list(range(start_row, end_row + 1))
        indices_expr = const(indices)
        index_params = [attrs["q_params"][0]] * 3
        index_name = base_name + f"_take_{i}"
        out_dtype = attrs.get("output_dtype", "float32")
        s_input = relay.qnn.op.csi_take(
            in_data,
            indices_expr,
            axis=2,
            out_dtype=out_dtype,
            q_params=index_params,
            mode="clip",
            layer_name=index_name,
        )
        s_padding = [0, padding[1], 0, padding[3]]
        if start_row == 0:
            s_padding[0] = padding[0]
        if end_row == d_h - 1:
            s_padding[2] = padding[2]

        s_conv_name = base_name + f"_{i}"
        attrs["padding"] = s_padding
        attrs["layer_name"] = s_conv_name
        attrs["groups"] = base_group
        if max_out_channel:
            if base_group > 1:
                raise Exception("Unsupport split group conv by out channel")
            s_conv = split_channel(s_input, weight, bias, attrs, max_out_channel)
        elif max_groups:
            s_conv = split_group(s_input, weight, bias, attrs, max_groups)
        else:
            if logging.DEBUG >= logger.getEffectiveLevel():
                s_inshape = infer_shape(s_input)
                split_params.append({"in_shape": s_inshape, "w_shape": weight.shape})
            s_conv = relay.qnn.op.csi_conv2d(s_input, const(weight), const(bias), **attrs)

        concat_tuple.append(s_conv)

    logger.debug("splitted conv %s %s by parmas: %s", base_name, in_shape, split_params)
    if len(concat_tuple) == 1:
        return s_conv
    q_params = [attrs["q_params"][-1]] * (len(concat_tuple) + 1)
    concat_name = base_name + "_concat" if base_name else base_name
    return relay.qnn.op.csi_concatenate(concat_tuple, 2, q_params, concat_name)


def split_depthwise_conv2d_input(
    in_data, weight, bias, attrs, sram_size, contain_weight, activation_byte, weight_byte
):
    padding = attrs["padding"]
    strides = attrs["strides"]
    dilation = attrs["dilation"]
    in_shape = infer_shape(in_data)
    w_shape = weight.shape
    d_w = in_shape[3]

    k_h = w_shape[2]
    k_w = w_shape[3]

    s_in_shape = [in_shape[0], in_shape[1], k_h, d_w]
    s_in_size = np.prod(s_in_shape) * activation_byte
    # top left down right
    s_padding = [padding[0], padding[1], 0, padding[3]]

    s_out_shape = _infer_shape(s_in_shape, w_shape, s_padding, dilation, strides)
    s_out_size = np.prod(s_out_shape) * activation_byte

    left_size = sram_size - (s_in_size + s_out_size)
    if contain_weight:
        k_size = np.prod(w_shape)
        left_size -= k_size * weight_byte

    if left_size >= 0:
        # try to split row
        max_row = get_max_row(
            sram_size,
            in_shape,
            w_shape,
            s_padding,
            dilation,
            strides,
            contain_weight,
            activation_byte,
            weight_byte,
        )
        return split_input_by_max_row(in_data, in_shape, weight, bias, attrs, max_row)

    # try to split group
    s_in_shape = [in_shape[0], 1, k_h, d_w]
    s_in_size = np.prod(s_in_shape) * activation_byte
    s_w_shape = [1, w_shape[1], k_h, k_w]
    s_out_shape = _infer_shape(s_in_shape, s_w_shape, s_padding, dilation, strides)
    s_out_size = np.prod(s_out_shape) * activation_byte

    left_size = sram_size - (s_in_size + s_out_size)
    s_k_size = np.prod(s_w_shape) * weight_byte
    if contain_weight:
        left_size -= s_k_size

    if left_size >= 0:
        # meet the needs of split grouop
        max_row = get_max_row(
            sram_size,
            s_in_shape,
            s_w_shape,
            s_padding,
            dilation,
            strides,
            contain_weight,
            activation_byte,
            weight_byte,
        )

        return split_input_by_max_row(in_data, in_shape, weight, bias, attrs, max_row, max_groups=1)

    # need split col
    raise Exception("Sram size is too small to split row. hhb unsupported column splitting now.")


def split_common_conv2d_input(
    in_data, weight, bias, attrs, sram_size, contain_weight, activation_byte, weight_byte
):
    w_shape = weight.shape
    in_shape = infer_shape(in_data)
    d_w = in_shape[3]
    k_h = w_shape[2]
    k_w = w_shape[3]

    padding = attrs["padding"]
    strides = attrs["strides"]
    dilation = attrs["dilation"]

    # compute s_out_size use in_data(k_h * dw* channel) and weight(total)
    # if s_out_size smaller than sram size, it needn't split column.
    # top left down right
    s_padding = [padding[0], padding[1], 0, padding[3]]

    s_in_shape = [in_shape[0], in_shape[1], k_h, d_w]
    s_out_shape = _infer_shape(s_in_shape, w_shape, s_padding, dilation, strides)
    s_out_size = np.prod(s_out_shape) * activation_byte
    s_in_size = np.prod(s_in_shape) * activation_byte

    left_size = sram_size - (s_in_size + s_out_size)
    if contain_weight:
        k_size = np.prod(w_shape) * weight_byte
        left_size -= k_size

    if left_size >= 0:
        # split in_data by row
        max_row = get_max_row(
            sram_size,
            in_shape,
            w_shape,
            s_padding,
            dilation,
            strides,
            contain_weight,
            activation_byte,
            weight_byte,
        )
        return split_input_by_max_row(in_data, in_shape, weight, bias, attrs, max_row)

    # try to split out channel
    s_w_shape = [1, w_shape[1], k_h, k_w]
    s_out_shape = _infer_shape(s_in_shape, s_w_shape, s_padding, dilation, strides)
    s_out_size = np.prod(s_out_shape) * activation_byte

    left_size = sram_size - (s_in_size + s_out_size)
    if contain_weight:
        s_k_size = np.prod(s_w_shape) * weight_byte
        left_size -= s_k_size

    if left_size >= 0:
        # meet the needs of split out channel
        max_row = get_max_row(
            sram_size,
            s_in_shape,
            s_w_shape,
            s_padding,
            dilation,
            strides,
            contain_weight,
            activation_byte,
            weight_byte,
        )

        return split_input_by_max_row(
            in_data, in_shape, weight, bias, attrs, max_row, max_out_channel=1
        )
    raise Exception("Sram size is too small to split row. hhb unsupported column splitting now.")


def split_group_conv2d_input(
    in_data, weight, bias, attrs, sram_size, contain_weight, activation_byte, weight_byte
):
    w_shape = weight.shape
    in_shape = infer_shape(in_data)
    d_w = in_shape[3]
    k_h = w_shape[2]
    k_w = w_shape[3]

    padding = attrs["padding"]
    strides = attrs["strides"]
    dilation = attrs["dilation"]

    # compute s_out_size use in_data(k_h * dw* channel) and weight(total)
    # if s_out_size smaller than sram size, it needn't split column.
    # top left down right
    s_padding = [padding[0], padding[1], 0, padding[3]]
    s_in_shape = [in_shape[0], in_shape[1], k_h, d_w]
    s_out_shape = _infer_shape(s_in_shape, w_shape, s_padding, dilation, strides, group=1)

    s_out_size = np.prod(s_out_shape) * activation_byte
    s_in_size = np.prod(s_in_shape) * activation_byte

    left_size = sram_size - (s_in_size + s_out_size)
    if contain_weight:
        k_size = np.prod(w_shape) * weight_byte
        left_size -= k_size

    if left_size >= 0:
        # split in_data by row
        max_row = get_max_row(
            sram_size,
            in_shape,
            w_shape,
            s_padding,
            dilation,
            strides,
            contain_weight,
            activation_byte,
            weight_byte,
        )
        return split_input_by_max_row(in_data, in_shape, weight, bias, attrs, max_row)

    # try to split group
    s_w_min = w_shape[0] // attrs["groups"]
    s_in_min = in_shape[1] // attrs["groups"]
    s_w_shape = [s_w_min, w_shape[1], k_h, k_w]
    s_in_shape = [in_shape[0], s_in_min, k_h, d_w]
    s_out_shape = _infer_shape(s_in_shape, s_w_shape, s_padding, dilation, strides)
    s_out_size = np.prod(s_out_shape) * activation_byte

    left_size = sram_size - (s_in_size + s_out_size)
    s_k_size = np.prod(s_w_shape) * weight_byte
    if contain_weight:
        left_size -= s_k_size

    if left_size >= 0:
        # meet the needs of split group
        max_row = get_max_row(
            sram_size,
            s_in_shape,
            s_w_shape,
            s_padding,
            dilation,
            strides,
            contain_weight,
            activation_byte,
            weight_byte,
        )

        return split_input_by_max_row(in_data, in_shape, weight, bias, attrs, max_row, 1)
    raise Exception("Sram size is too small to split row. hhb unsupported column splitting now.")


class SramSizeSpliter(ExprMutator):
    """Split common convolution by sram size"""

    def __init__(self, config):
        super(SramSizeSpliter, self).__init__()
        self.config = config
        self.sram_size = config.h_sram_size
        self.contain_weight = config.h_contain_weight
        self.activation_byte = config.nbit_input // 8
        self.weight_byte = config.nbit_weight // 8

    def visit_call(self, call):
        op_args = [self.visit(arg) for arg in call.args]
        if call.op.name == "qnn.csi.conv2d":
            in_data = op_args[0]
            conv_attrs = _qnn_attrs(call.attrs)
            in_shape = infer_shape(in_data)
            o_shape = infer_shape(call)
            w_shape = op_args[1].data.asnumpy().shape

            in_size = np.prod(in_shape) * self.activation_byte
            out_size = np.prod(o_shape) * self.activation_byte

            total_size = in_size + out_size
            if self.contain_weight:
                w_size = np.prod(w_shape) * self.weight_byte
                total_size += w_size

            # needn't split
            if total_size <= self.sram_size:
                return Call(call.op, op_args, call.attrs)

            weight = op_args[1].data.asnumpy()
            bias = op_args[2].data.asnumpy()
            is_depthwise = is_depthwise_conv2d(
                in_shape, "NCHW", w_shape, "OIHW", conv_attrs["groups"]
            )

            s_out_size = np.prod(o_shape[2:]) * self.activation_byte
            s_w_size = np.prod(w_shape[2:]) * self.weight_byte
            # for depthwise
            if is_depthwise:
                # NCHW
                s_in_size = np.prod(in_shape[2:]) * self.activation_byte
                max_groups = np.floor_divide(
                    self.sram_size, (s_in_size + s_out_size + s_w_size)
                ).astype(int)
                if max_groups > 0:
                    logger.debug("split depthwise conv by max groups:%s", max_groups)
                    logger.debug(
                        "original dw conv shaps: in_shape=%s, w_shape=%s", in_shape, w_shape
                    )
                    return split_group(in_data, weight, bias, conv_attrs, max_groups)

                return split_depthwise_conv2d_input(
                    in_data,
                    weight,
                    bias,
                    conv_attrs,
                    self.sram_size,
                    self.contain_weight,
                    self.activation_byte,
                    self.weight_byte,
                )

            # for group convolution
            if conv_attrs["groups"] > 1:
                # group conv can split as common single conv.
                # it can be use directly if sram size larger than single group size.
                s_group_size = total_size // conv_attrs["groups"]
                if s_group_size <= self.sram_size:
                    max_groups = np.floor_divide(self.sram_size, s_group_size).astype(int)

                    logger.debug("split group conv by max groups:%s", max_groups)
                    logger.debug(
                        "original group conv shaps: in_shape=%s, w_shape=%s", in_shape, w_shape
                    )
                    return split_group(in_data, weight, bias, conv_attrs, max_groups)

                return split_group_conv2d_input(
                    in_data,
                    weight,
                    bias,
                    conv_attrs,
                    self.sram_size,
                    self.contain_weight,
                    self.activation_byte,
                    self.weight_byte,
                )

            s_out_size = np.prod(o_shape[1:]) * self.activation_byte
            s_w_size = np.prod(w_shape[1:]) * self.weight_byte
            # for common convolution
            max_out_channel = np.floor_divide(
                self.sram_size - in_size, (s_out_size + s_w_size)
            ).astype(int)
            if max_out_channel > 0:

                logger.debug("split common conv by max groups:%s", max_out_channel)
                logger.debug(
                    "original common conv shaps: in_shape=%s, w_shape=%s", in_shape, w_shape
                )
                return split_channel(in_data, weight, bias, conv_attrs, max_out_channel)

            return split_common_conv2d_input(
                in_data,
                weight,
                bias,
                conv_attrs,
                self.sram_size,
                self.contain_weight,
                self.activation_byte,
                self.weight_byte,
            )

        if call.op.name == "qnn.csi.relu":
            if isinstance(op_args[0], Call) and op_args[0].op.name == "qnn.csi.concatenate":
                return split_relu(call, op_args)

        return Call(call.op, op_args, call.attrs)

    def visit_function(self, fn):
        new_params = [self.visit(x) for x in fn.params]
        new_body = self.visit(fn.body)
        return function.Function(list(new_params), new_body)


@function_pass(opt_level=1)
class ConvSpliter:
    r"""
    split convlution according to different conditions:
        max_groups, max_out_channel, max_kernel_size, sram_size
    """

    def __init__(self, crt_config):
        self.max_groups = crt_config.h_max_groups
        self.max_out_channel = crt_config.h_max_out_channel
        self.max_kernel_size = crt_config.h_max_kernel_size
        self.sram_size = crt_config.h_sram_size
        self.contain_weight = crt_config.h_contain_weight
        self.config = crt_config

    def transform_function(self, func, mod, ctx):
        """patten and split op"""
        if self.max_groups:
            logger.debug("split by max groups: max_groups = %s", self.max_groups)
            mod["main"] = MaxGroupSpliter(self.max_groups).visit(mod["main"])
        if self.max_out_channel:
            logger.debug("split by out groups: max_out_channel = %s", self.max_out_channel)
            mod["main"] = OutChannelSpliter(self.max_out_channel).visit(mod["main"])
        if self.max_kernel_size:
            logger.debug("split by kernel size: max_kernel_size = %s", self.max_kernel_size)
            mod["main"] = KernelSizeSpliter(self.config, self.max_kernel_size).visit(mod["main"])
        if self.sram_size:
            logger.debug("split by sram size: max_sram_size = %s", self.sram_size)
            mod["main"] = SramSizeSpliter(self.config).visit(mod["main"])
        return mod["main"]
