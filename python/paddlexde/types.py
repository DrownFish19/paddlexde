from typing import Sequence, Union

from paddle import Tensor
from paddle.nn import Layer

Tensor = Tensor
Tensors = Sequence[Tensor]
TensorOrTensors = Union[Tensor, Tensors]

Layer = Layer
Layers = Sequence[Layer]
LayerOrLayers = Union[Layer, Layer]
LayerOrFunction = Union[Layer, callable]

Scalar = Union[float, Tensor]
Vector = Union[Sequence[float], Tensor]

TupleOrTensor = Union[tuple, Tensor]