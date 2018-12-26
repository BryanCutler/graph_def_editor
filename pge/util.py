# Copyright 2018 IBM. All Rights Reserved.
# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Utility functions for pge
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import re
from typing import Any

import numpy as np
from six import iteritems
import tensorflow as tf

from pge import graph
from pge import node
from pge import tensor

__all__ = [
  "make_list_of_op",
  "make_list_of_t",
  "get_generating_ops",
  "get_consuming_ops",
  "ControlOutputs",
  "placeholder_name",
  "make_placeholder_from_tensor",
  "make_placeholder_from_dtype_and_shape",
]


# The graph editor sometimes need to create placeholders, they are named
# "geph_*". "geph" stands for Graph-Editor PlaceHolder.
_DEFAULT_PLACEHOLDER_PREFIX = "geph"


def concatenate_unique(la, lb):
  """Add all the elements of `lb` to `la` if they are not there already.

  The elements added to `la` maintain ordering with respect to `lb`.

  Args:
    la: List of Python objects.
    lb: List of Python objects.
  Returns:
    `la`: The list `la` with missing elements from `lb`.
  """
  la_set = set(la)
  for l in lb:
    if l not in la_set:
      la.append(l)
      la_set.add(l)
  return la


# TODO(fkp): very generic code, it should be moved in a more generic place.
class ListView(object):
  """Immutable list wrapper.

  This class is strongly inspired by the one in tf.Operation.
  """

  def __init__(self, list_):
    if not isinstance(list_, list):
      raise TypeError("Expected a list, got: {}.".format(type(list_)))
    self._list = list_

  def __iter__(self):
    return iter(self._list)

  def __len__(self):
    return len(self._list)

  def __bool__(self):
    return bool(self._list)

  # Python 3 wants __bool__, Python 2.7 wants __nonzero__
  __nonzero__ = __bool__

  def __getitem__(self, i):
    return self._list[i]

  def __add__(self, other):
    if not isinstance(other, list):
      other = list(other)
    return list(self) + other


# TODO(fkp): very generic code, it should be moved in a more generic place.
def is_iterable(obj):
  """Return true if the object is iterable."""
  if isinstance(obj, node.Node):
    return False
  try:
    _ = iter(obj)
  except Exception:  # pylint: disable=broad-except
    return False
  return True


def flatten_tree(tree, leaves=None):
  """Flatten a tree into a list.
  Args:
    tree: iterable or not. If iterable, its elements (child) can also be
      iterable or not.
    leaves: list to which the tree leaves are appended (None by default).
  Returns:
    A list of all the leaves in the tree.
  """
  if leaves is None:
    leaves = []
  if isinstance(tree, dict):
    for _, child in iteritems(tree):
      flatten_tree(child, leaves)
  elif is_iterable(tree):
    for child in tree:
      flatten_tree(child, leaves)
  else:
    leaves.append(tree)
  return leaves


def transform_tree(tree, fn, iterable_type=tuple):
  """Transform all the nodes of a tree.
  Args:
    tree: iterable or not. If iterable, its elements (child) can also be
      iterable or not.
    fn: function to apply to each leaves.
    iterable_type: type use to construct the resulting tree for unknown
      iterable, typically `list` or `tuple`.
  Returns:
    A tree whose leaves has been transformed by `fn`.
    The hierarchy of the output tree mimics the one of the input tree.
  """
  if is_iterable(tree):
    if isinstance(tree, dict):
      res = tree.__new__(type(tree))
      res.__init__(
        (k, transform_tree(child, fn)) for k, child in iteritems(tree))
      return res
    elif isinstance(tree, tuple):
      # NamedTuple?
      if hasattr(tree, "_asdict"):
        res = tree.__new__(type(tree), **transform_tree(tree._asdict(), fn))
      else:
        res = tree.__new__(type(tree),
                           (transform_tree(child, fn) for child in tree))
      return res
    elif isinstance(tree, collections.Sequence):
      res = tree.__new__(type(tree))
      res.__init__(transform_tree(child, fn) for child in tree)
      return res
    else:
      return iterable_type(transform_tree(child, fn) for child in tree)
  else:
    return fn(tree)


def check_graphs(*args):
  """Check that all the elements in args belong to the same graph.

  Args:
    *args: a list of object with a obj.graph property.
  Raises:
    ValueError: if all the elements do not belong to the same graph.
  """
  g = None
  for i, sgv in enumerate(args):
    if g is None and sgv.graph is not None:
      g = sgv.graph
    elif sgv.graph is not None and sgv.graph is not g:
      raise ValueError("Argument[{}]: Wrong graph!".format(i))


def get_unique_graph(tops, check_types=None, none_if_empty=False):
  """Return the unique graph used by the all the elements in tops.

  Args:
    tops: list of elements to check (usually a list of pge.Tensor). Or a
      pge.Graph.
    check_types: check that the element in tops are of given type(s). If None,
      the types (pge.Node, pge.Tensor) are used.
    none_if_empty: don't raise an error if tops is an empty list, just return
      None.
  Returns:
    The unique graph used by all the tops.
  Raises:
    TypeError: if tops is not a iterable of tf.Operation.
    ValueError: if the graph is not unique.
  """
  if isinstance(tops, graph.Graph):
    return tops
  if not is_iterable(tops):
    raise TypeError("{} is not iterable".format(type(tops)))
  if check_types is None:
    check_types = (node.Node, tensor.Tensor,)
  elif not is_iterable(check_types):
    check_types = (check_types,)
  g = None
  for op in tops:
    if not isinstance(op, check_types):
      raise TypeError("Expected a type in ({}), got: {}".format(", ".join([str(
          t) for t in check_types]), type(op)))
    if g is None:
      g = op.graph
    elif g is not op.graph:
      raise ValueError("Operation {} does not belong to given graph".format(op))
  if g is None and not none_if_empty:
    raise ValueError("Can't find the unique graph of an empty list")
  return g


def make_list_of_op(ops, check_graph=True, allow_graph=True, ignore_ts=False):
  """Convert ops to a list of `pge.Node`.

  Args:
    ops: can be an iterable of `pge.Node`, a `tf.Graph` or a single
      Node.
    check_graph: if `True` check if all the nodes belong to the same graph.
    allow_graph: if `False` a `pge.Graph` cannot be converted.
    ignore_ts: if True, silently ignore `tf.Tensor`.
  Returns:
    A newly created list of `tf.Operation`.
  Raises:
    TypeError: if ops cannot be converted to a list of `tf.Operation` or,
     if `check_graph` is `True`, if all the ops do not belong to the
     same graph.
  """
  if isinstance(ops, graph.Graph):
    if allow_graph:
      return ops.nodes
    else:
      raise TypeError("allow_graph is False: cannot convert a tf.Graph.")
  else:
    if not is_iterable(ops):
      ops = [ops]
    if not ops:
      return []
    if check_graph:
      check_types = None if ignore_ts else node.Node
      get_unique_graph(ops, check_types=check_types)
    return [op for op in ops if isinstance(op, node.Node)]


def make_list_of_t(ts, check_graph=True, allow_graph=True, ignore_ops=False):
  """Convert ts to a list of `tf.Tensor`.

  Args:
    ts: can be an iterable of `pge.Tensor`, a `pge.Graph` or a single tensor.
    check_graph: if `True` check if all the tensors belong to the same graph.
    allow_graph: if `False` a `pge.Graph` cannot be converted.
    ignore_ops: if `True`, silently ignore `tf.Operation`.
  Returns:
    A newly created list of `pge.Tensor`.
  Raises:
    TypeError: if `ts` cannot be converted to a list of `pge.Tensor` or,
     if `check_graph` is `True`, if all the ops do not belong to the same graph.
  """
  if isinstance(ts, graph.Graph):
    if allow_graph:
      return ts.tensors
    else:
      raise TypeError("allow_graph is False: cannot convert a pge.Graph.")
  else:
    if not is_iterable(ts):
      ts = [ts]
    if not ts:
      return []
    if check_graph:
      check_types = None if ignore_ops else (tensor.Tensor,)
      get_unique_graph(ts, check_types=check_types)
    return [t for t in ts if isinstance(t, tensor.Tensor)]


def get_generating_ops(ts):
  """Return all the generating ops of the tensors in `ts`.

  Args:
    ts: a list of `pge.Tensor`
  Returns:
    A list of all the `pge.Node` objects that represent the generating
    `tf.Operation`s of the tensors in `ts`.
  Raises:
    TypeError: if `ts` cannot be converted to a list of `pge.Tensor`.
  """
  ts = make_list_of_t(ts, allow_graph=False)
  return [t.node for t in ts]


def get_consuming_ops(ts):
  """Return all the consuming ops of the tensors in ts.

  Args:
    ts: a list of `pge.Tensor`
  Returns:
    A list of all the `pge.Node` objects that represent the consuming
    `tf.Operation`s of the tensors in `ts`.
  Raises:
    TypeError: if ts cannot be converted to a list of `pge.Tensor`.
  """
  ts = make_list_of_t(ts, allow_graph=False)
  ops = []
  for t in ts:
    for op in t.consumers:
      if op not in ops:
        ops.append(op)
  return ops


class ControlOutputs(object):
  """The control outputs topology."""

  def __init__(self, g: 'graph.Graph'):
    """Create a dictionary of control-output dependencies.

    Args:
      g: a `pge.Graph`.
    Returns:
      A dictionary where a key is `pge.Node` object and the corresponding value
      is a list of all the ops which have the keys one of their control-input
      dependencies.
    Raises:
      TypeError: graph is not a `pge.Graph`.
    """
    if not isinstance(g, graph.Graph):
      raise TypeError("Expected a pge.Graph, got: {}".format(type(g)))
    self._control_outputs = {}
    self._graph = g
    self._version = None
    self._build()

  def update(self):
    """Update the control outputs if the graph has changed."""
    if self._version != self._graph.version:
      self._build()
    return self

  def _build(self):
    """Build the control outputs dictionary."""
    self._control_outputs.clear()
    for node in self._graph.nodes:
      for control_input in node.control_inputs:
        if control_input not in self._control_outputs:
          self._control_outputs[control_input] = []
        if node not in self._control_outputs[control_input]:
          self._control_outputs[control_input].append(node)
    self._version = self._graph.version

  def get_all(self):
    return self._control_outputs

  def get(self, op):
    """return the control outputs of op."""
    if op in self._control_outputs:
      return self._control_outputs[op]
    else:
      return ()

  @property
  def graph(self):
    return self._graph


def scope_finalize(scope):
  if scope and scope[-1] != "/":
    scope += "/"
  return scope


def scope_dirname(scope):
  slash = scope.rfind("/")
  if slash == -1:
    return ""
  return scope[:slash + 1]


def scope_basename(scope):
  slash = scope.rfind("/")
  if slash == -1:
    return scope
  return scope[slash + 1:]


def placeholder_name(t=None, scope=None, prefix=_DEFAULT_PLACEHOLDER_PREFIX):
  """Create placeholder name for the graph editor.

  Args:
    t: optional `pge.Tensor` on which the placeholder operation's name will be
      based on
    scope: absolute scope with which to prefix the placeholder's name. None
      means that the scope of t is preserved. "" means the root scope.
    prefix: placeholder name prefix.
  Returns:
    A new placeholder name prefixed by "geph". Note that "geph" stands for
      Graph Editor PlaceHolder. This convention allows to quickly identify the
      placeholder generated by the Graph Editor.
  Raises:
    TypeError: if t is not None or a tf.Tensor.
  """
  if scope is not None:
    scope = scope_finalize(scope)
  if t is not None:
    if not isinstance(t, tensor.Tensor):
      raise TypeError("Expected a pge.Tensor, got: {}".format(type(t)))
    op_dirname = scope_dirname(t.node.name)
    op_basename = scope_basename(t.node.name)
    if scope is None:
      scope = op_dirname

    if op_basename.startswith("{}__".format(prefix)):
      ph_name = op_basename
    else:
      ph_name = "{}__{}_{}".format(prefix, op_basename, t.value_index)

    return scope + ph_name
  else:
    if scope is None:
      scope = ""
    return "{}{}".format(scope, prefix)


def _make_placeholder(g: 'g.Graph',
                      dtype: tf.DType,
                      shape,
                      name: str):
  """Shared code for make_placeholder*() functions.

  Args:
    g: Surrogate object for the graph into which the placeholder should
      be placed.
    name: Name for the op to create
    dtype: Data type for the placeholder
    shape: Shape of the placeholder's output (exact type TBD)

  Returns:
     newly created mutable Node that wraps the placholder op.
  """
  ret = g.add_node(name, op_name="Placeholder")
  ret.add_attr("dtype", dtype)
  ret.add_attr("shape", tf.TensorShape(shape))
  return ret


def make_placeholder_from_tensor(g: 'graph.Graph', t: tensor.Tensor, scope=None,
                                 prefix=_DEFAULT_PLACEHOLDER_PREFIX):
  """Create a `pge.Node` representing a `tf.placeholder` for the Graph Editor.

  Note that the correct graph scope must be set by the calling function.

  Args:
    g: A `pge.Graph` object in which the placeholder should go.
    t: a `pge.Tensor` whose name will be used to create the placeholder
    scope: absolute scope within which to create the placeholder. None
      means that the scope of `t` is preserved. `""` means the root scope.
    prefix: placeholder name prefix.
  Returns:
    A newly created `pge.Node` that represents the `tf.placeholder`.
  Raises:
    TypeError: if `t` is not `None` or a `tf.Tensor`.
  """
  return _make_placeholder(g, dtype=t.dtype, shape=t.shape,
                           name=placeholder_name(t, scope=scope,
                                                 prefix=prefix))


def make_placeholder_from_dtype_and_shape(g, dtype, shape=None, scope=None,
                                          prefix=_DEFAULT_PLACEHOLDER_PREFIX):
  """Create a `pge.Node` representing a `tf.placeholder` for the Graph Editor.

  Note that the correct graph scope must be set by the calling function.
  The placeholder is named using the function placeholder_name (with no
  tensor argument).

  Args:
    g: A `pge.Graph` object in which the placeholder should go.
    dtype: the tensor type.
    shape: the tensor shape (optional).
    scope: absolute scope within which to create the placeholder. None
      means that the scope of t is preserved. "" means the root scope.
    prefix: placeholder name prefix.
  Returns:
    A newly created tf.placeholder.
  """
  return _make_placeholder(g,
                           dtype=dtype, shape=shape,
                           name=placeholder_name(scope=scope, prefix=prefix))


_INTERNAL_VARIABLE_RE = re.compile(r"^__\w+__$")


def get_predefined_collection_names():
  """Return all the predefined collection names."""
  return [getattr(tf.GraphKeys, key) for key in dir(tf.GraphKeys)
          if not _INTERNAL_VARIABLE_RE.match(key)]


def find_corresponding_elem(target, dst_graph, dst_scope="", src_scope=""):
  """Find corresponding op/tensor in a different graph.

  Args:
    target: A `tf.Tensor` or a `tf.Operation` belonging to the original graph.
    dst_graph: The graph in which the corresponding graph element must be found.
    dst_scope: A scope which is prepended to the name to look for.
    src_scope: A scope which is removed from the original of `target` name.

  Returns:
    The corresponding tf.Tensor` or a `tf.Operation`.

  Raises:
    ValueError: if `src_name` does not start with `src_scope`.
    TypeError: if `target` is not a `tf.Tensor` or a `tf.Operation`
    KeyError: If the corresponding graph element cannot be found.
  """
  src_name = target.name
  if src_scope:
    src_scope = scope_finalize(src_scope)
    if not src_name.startswidth(src_scope):
      raise ValueError("{} does not start with {}".format(src_name, src_scope))
    src_name = src_name[len(src_scope):]

  dst_name = src_name
  if dst_scope:
    dst_scope = scope_finalize(dst_scope)
    dst_name = dst_scope + dst_name

  if isinstance(target, tensor.Tensor):
    return dst_graph.get_tensor_by_name(dst_name)
  if isinstance(target, node.Node):
    return dst_graph[dst_name]
  raise TypeError("Expected tf.Tensor or tf.Operation, got: {}", type(target))


def find_corresponding(targets, dst_graph, dst_scope="", src_scope=""):
  """Find corresponding ops/tensors in a different graph.

  `targets` is a Python tree, that is, a nested structure of iterable
  (list, tupple, dictionary) whose leaves are instances of
  `tf.Tensor` or `tf.Operation`

  Args:
    targets: A Python tree containing `tf.Tensor` or `tf.Operation`
      belonging to the original graph.
    dst_graph: The graph in which the corresponding graph element must be found.
    dst_scope: A scope which is prepended to the name to look for.
    src_scope: A scope which is removed from the original of `top` name.

  Returns:
    A Python tree containin the corresponding tf.Tensor` or a `tf.Operation`.

  Raises:
    ValueError: if `src_name` does not start with `src_scope`.
    TypeError: if `top` is not a `tf.Tensor` or a `tf.Operation`
    KeyError: If the corresponding graph element cannot be found.
  """
  def func(top):
    return find_corresponding_elem(top, dst_graph, dst_scope, src_scope)
  return transform_tree(targets, func)


def _python_type_to_attr_list_elem(list_value: tf.AttrValue.ListValue,
                                   elem: Any):
  """
  Subroutine of python_type_to_attr_value(). Converts one element of a Python
  list to one element of a `tf.AttrValue.ListValue` protobuf.

  Args:
    list_value: ListValue proto being populated for use within an AttrValue
      proto. Modified in place.
    elem: Original value to convert.
  """
  if isinstance(elem, str):
    list_value.s.append(tf.compat.as_bytes(elem))
  elif isinstance(elem, int):
    list_value.i.append(elem)
  elif isinstance(elem, float):
    list_value.f.append(elem)
  elif isinstance(elem, bool):
    list_value.b.append(elem)
  elif isinstance(elem, tf.DType):
    list_value.type.append(elem.as_datatype_enum)
  elif isinstance(elem, tf.TensorShape):
    list_value.shape.append(elem.as_proto())
  elif isinstance(elem, np.ndarray):
    list_value.tensor.append(tf.make_tensor_proto(values=elem))
  # TODO(frreiss): Populate the "func" field of the union here
  else:
    raise ValueError("Don't know how to convert a {} to "
                     "tf.AttrValue.ListValue".format(type(elem)))


def python_type_to_attr_value(value: Any) -> tf.AttrValue:
  """
  Convert a Python object or scalar value to a TensorFlow `tf.AttrValue`
  protocol buffer message.

  Args:
    value: Python object to be converted

  Returns:
    An AttrValue object that wraps the contents of `value` in the most
    appropriate way available.
  """
  if isinstance(value, list) or isinstance(value, tuple):
    if 0 == len(value):
      return tf.AttrValue(list=tf.AttrValue.ListValue())
    else:
      # Nonempty list
      list_value = tf.AttrValue.ListValue()
      for elem in value:
        # TODO(frreiss): Should we disallow heterogeneous types in lists?
        _python_type_to_attr_list_elem(list_value, elem)
      return tf.AttrValue(list=list_value)
  elif isinstance(value, tf.AttrValue):
    # TODO(frreiss): Should this case result in an error?
    return value
  # Scalar types, in the order they appear in the .proto file
  elif isinstance(value, str):
    return tf.AttrValue(s=tf.compat.as_bytes(value))
  elif isinstance(value, int):
    return tf.AttrValue(i=value)
  elif isinstance(value, float):
    return tf.AttrValue(f=value)
  elif isinstance(value, bool):
    return tf.AttrValue(b=value)
  elif isinstance(value, tf.DType):
    return tf.AttrValue(type=value.as_datatype_enum)
  elif isinstance(value, tf.TensorShape):
    return tf.AttrValue(shape=value.as_proto())
  elif isinstance(value, np.ndarray):
    return tf.AttrValue(tensor=tf.make_tensor_proto(values=value))
  # TODO(frreiss): Populate the "func" and "placeholder" fields of the union
  #  here
  else:
    raise ValueError("Don't know how to convert a {} to "
                     "tf.AttrValue".format(type(value)))


def attr_value_to_python_type(attr_value: tf.AttrValue) -> Any:
  """
  Inverse of python_type_to_attr_value().

  Args:
    attr_value: Protocol buffer version of a node's attribute value

  Returns:
    A Python object or built-in type corresponding to the field in
    `attr_value` that is in use.
  """
  # TODO(frreiss): Handle AttrValues that are lists
  if attr_value.HasField("s"):          # str
    # TODO(frreiss): Should we return the binary value here?
    return tf.compat.as_str(attr_value.s)
  elif attr_value.HasField("i"):        # int
    return attr_value.i
  elif attr_value.HasField("f"):        # float
    return attr_value.f
  elif attr_value.HasField("b"):        # bool
    return attr_value.b
  elif attr_value.HasField("type"):     # DType
    return tf.DType(attr_value.type)
  elif attr_value.HasField("shape"):    # TensorShape
    # Undocumented behavior of public API: tf.TensorShape constructor accepts
    # a TensorShapeProto.
    return tf.TensorShape(attr_value.shape)
  elif attr_value.HasField("tensor"):   # TensorProto
    return tf.make_ndarray(attr_value.tensor)
  # TODO(frreiss): Convert the "func" and "placeholder" fields of the union
  #  here
  else:
    raise ValueError("Don't know how to convert AttrValue {} to "
                     "a Python object".format(attr_value))
