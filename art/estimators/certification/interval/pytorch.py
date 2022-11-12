# MIT License
#
# Copyright (C) The Adversarial Robustness Toolbox (ART) Authors 2022
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the "Software"), to deal in the Software without restriction, including without limitation the
# rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit
# persons to whom the Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all copies or substantial portions of the
# Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
# WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
# TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""
This module implements ... proposed in ....

| Paper link: ...
"""

from typing import List, Optional, Tuple, Union, Any, TYPE_CHECKING

import logging
import math
import warnings
import sys

import numpy as np
import torch

from art.estimators.certification.interval.interval import IntervalConv2D, IntervalDenseLayer, IntervalReLU, convert_to_interval
from art.estimators.classification.pytorch import PyTorchClassifier

if sys.version_info < (3, 8):
    from functools import reduce

if TYPE_CHECKING:
    from art.utils import CLIP_VALUES_TYPE, PREPROCESSING_TYPE
    from art.defences.preprocessor import Preprocessor
    from art.defences.postprocessor import Postprocessor


class ConvertedModel(torch.nn.Module):
    """
    Class which converts the supplied pytorch model into an equivalent model
    which uses abstract operations
    """

    def __init__(self, model: "torch.nn.Module", channels_first: bool, input_shape: Tuple[int, ...]):
        super().__init__()
        modules = []
        self.interim_shapes = []
        self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        self.forward_mode: str
        self.forward_mode = "abstract"

        # pylint: disable=W0613
        def forward_hook(input_module, hook_input, hook_output):
            modules.append(input_module)
            self.interim_shapes.append(tuple(hook_input[0].shape))

        for module in model.children():
            module.register_forward_hook(forward_hook)

        if channels_first:
            input_for_hook = torch.rand(input_shape).to(self.device)
        else:
            raise ValueError("Please provide data in channels first format")

        input_for_hook = torch.unsqueeze(input_for_hook, dim=0)
        model(input_for_hook)  # hooks are fired sequentially from model input to the output

        self.ops = torch.nn.ModuleList()
        for module, shapes in zip(modules, self.interim_shapes):
            print("registered", type(module))
            if isinstance(module, torch.nn.modules.conv.Conv2d):
                interval_conv = IntervalConv2D(
                    input_shape=shapes,
                    in_channels=module.in_channels,
                    out_channels=module.out_channels,
                    kernel_size=module.kernel_size,  # type: ignore
                    stride=module.stride,  # type: ignore
                    supplied_input_weights=torch.tensor(module.weight.data.cpu().detach().numpy()),
                    supplied_input_bias=torch.tensor(module.bias.data.cpu().detach().numpy()),
                    # dilation=module.dilation,  # type: ignore
                    # padding=module.padding,  # type: ignore
                )

                self.ops.append(interval_conv)
            elif isinstance(module, torch.nn.modules.linear.Linear):
                interval_dense = IntervalDenseLayer(in_features=module.in_features, out_features=module.out_features)
                interval_dense.weight.data = module.weight.data.to(self.device)
                interval_dense.bias.data = module.bias.data.to(self.device)
                self.ops.append(interval_dense)

            elif isinstance(module, torch.nn.modules.activation.ReLU):
                self.ops.append(IntervalReLU(device=self.device))
            else:
                raise ValueError("Supported Operations are Conv2D, Linear, and RelU")

        for op_num, op in enumerate(self.ops):
            # as reshapes are not modules we infer when the reshape from convolutional to dense occurs
            if isinstance(op, IntervalDenseLayer):
                # if the preceeding op was a convolution:
                if isinstance(self.ops[op_num - 1], IntervalConv2D):
                    self.reshape_op_num = op_num
                    print("Inferred reshape on op num", op_num)
                # if the preceeding op was a relu and the one before the activation was a convolution
                if isinstance(self.ops[op_num - 1], IntervalReLU) and isinstance(self.ops[op_num - 2], IntervalConv2D):
                    self.reshape_op_num = op_num
                    print("Inferred reshape on op num", op_num)

    def forward(self, x) -> "torch.Tensor":
        """
        Performs the neural network forward pass, either using abstract operations or concrete ones
        depending on the value of self.forward_mode

        :param cent: input data, either regular data if running in concrete mode, or the zonotope bias term.
        :param eps: zonotope error terms if running in abstract mode
        :return: model predictions, with zonotope error terms if running in abstract mode
        """
        if self.forward_mode == "concrete":
            return self.concrete_forward(x)
        if self.forward_mode == "abstract":
            return self.abstract_forward(x)
            # if eps is not None:
                # return self.abstract_forward(x)
                # return out_cent, out_eps
            # raise ValueError("for abstract forward mode, please provide both cent and eps")
        raise ValueError("forward_mode must be set to abstract or concrete")

    def abstract_forward(self, x_interval: np.ndarray) -> "torch.Tensor":
        """
        Do the forward pass through the NN with the given error terms and zonotope center.

        :param x_interval:
        :return: A tuple, the first element being the zonotope center vector.
                 The second is the zonotope error terms/coefficients.
        """

        x_interval = torch.from_numpy(x_interval.astype("float32")).to(self.device)

        for op_num, op in enumerate(self.ops):
            # as reshapes are not modules we infer when the reshape from convolutional to dense occurs
            if self.reshape_op_num == op_num:
                if True:
                    x_interval = x_interval.permute(0, 1, 3, 4, 2)
                x_interval = x_interval.reshape((x_interval.shape[0], 2, -1))
            x_interval = op.abstract_forward(x_interval)
        return x_interval

    def concrete_forward(self, in_x: Union[np.ndarray, "torch.Tensor"]) -> "torch.Tensor":
        """
        Do the forward pass using the concrete operations

        :param in_x: regular (concrete) data.
        """
        if isinstance(in_x, np.ndarray):
            x = torch.from_numpy(in_x.astype("float32")).to(self.device)
        else:
            x = in_x

        for op_num, (op, shape_assertion) in enumerate(zip(self.ops, self.interim_shapes)):
            # as reshapes are not modules we infer when the reshape from convolutional to dense occurs
            if self.reshape_op_num == op_num:
                if True:
                    x = x.permute(0, 2, 3, 1)
                x = x.reshape((x.shape[0], -1))
            x = op.concrete_forward(x)
        return x

    def set_forward_mode(self, mode: str) -> None:
        """
        Helper function to set the forward mode of the model

        :param mode: either concrete or abstract signifying how to run the forward pass
        """
        assert mode in {"concrete", "abstract"}
        self.forward_mode = mode


class PytorchInterval(PyTorchClassifier):
    """
    Implementation of Interval based certification for neural network robustness.
    We use the interval (also called box) representation of a datapoint as it travels through the network
    to then verify if it can have its class changed given a certain perturbation.

    | Paper link:
    """

    estimator_params = PyTorchClassifier.estimator_params

    def __init__(
        self,
        model: "torch.nn.Module",
        loss: "torch.nn.modules.loss._Loss",
        input_shape: Tuple[int, ...],
        nb_classes: int,
        optimizer: Optional["torch.optim.Optimizer"] = None,
        channels_first: bool = True,
        clip_values: Optional["CLIP_VALUES_TYPE"] = None,
        preprocessing_defences: Union["Preprocessor", List["Preprocessor"], None] = None,
        postprocessing_defences: Union["Postprocessor", List["Postprocessor"], None] = None,
        preprocessing: "PREPROCESSING_TYPE" = (0.0, 1.0),
        device_type: str = "gpu",
        concrete_to_interval: Optional[Any] = None,
    ):
        """
        Create a certifier based on the zonotope domain.

        :param model: PyTorch model. The output of the model can be logits, probabilities or anything else. Logits
               output should be preferred where possible to ensure attack efficiency.
        :param loss: The loss function for which to compute gradients for training. The target label must be raw
               categorical, i.e. not converted to one-hot encoding.
        :param input_shape: The shape of one input instance.
        :param nb_classes: The number of classes of the model.
        :param optimizer: The optimizer used to train the classifier.
        :param channels_first: Set channels first or last.
        :param clip_values: Tuple of the form `(min, max)` of floats or `np.ndarray` representing the minimum and
               maximum values allowed for features. If floats are provided, these will be used as the range of all
               features. If arrays are provided, each value will be considered the bound for a feature, thus
               the shape of clip values needs to match the total number of features.
        :param preprocessing_defences: Preprocessing defence(s) to be applied by the classifier.
        :param postprocessing_defences: Postprocessing defence(s) to be applied by the classifier.
        :param preprocessing: Tuple of the form `(subtrahend, divisor)` of floats or `np.ndarray` of values to be
               used for data preprocessing. The first value will be subtracted from the input. The input will then
               be divided by the second one.
        :param device_type: Type of device on which the classifier is run, either `gpu` or `cpu`.
        :param concrete_to_interval:  Optional argument. Function which takes in a concrete data point and the bound
                                      and converts the datapoint to the concrete_to_interval domain via:

                                                processed_sample, eps_bound = concrete_to_interval(sample, bound)

                                      where processed_sample is the zonotope bias term, and eps_bound are the
                                      associated error terms.
                                      If left as None, by default we apply the bound to every feature equally and
                                      adjust the zonotope such that it remains in the 0 - 1 range.
        """

        warnings.warn(
            "\nThis estimator does not support networks which have dense layers before convolutional. "
            "We currently infer a reshape when a neural network goes from convolutional layers to "
            "dense layers. If your use case does not fall into this pattern then consider "
            "directly building a certifier network with the "
            "custom layers found in art.estimators.certification.interval.interval.py\n"
        )
        converted_model = ConvertedModel(model, channels_first, input_shape)

        if TYPE_CHECKING:
            converted_optimizer: Union[torch.optim.Adam, torch.optim.SGD, None]

        if optimizer is not None:
            opt_state_dict = optimizer.state_dict()
            if isinstance(optimizer, torch.optim.Adam):
                logging.info("Converting Adam Optimiser")
                converted_optimizer = torch.optim.Adam(converted_model.parameters(), lr=1e-4)
            elif isinstance(optimizer, torch.optim.SGD):
                logging.info("Converting SGD Optimiser")
                converted_optimizer = torch.optim.SGD(converted_model.parameters(), lr=1e-4)
            else:
                raise ValueError("Optimiser not supported for conversion")

            converted_optimizer.load_state_dict(opt_state_dict)
        else:
            converted_optimizer = None

        self.concrete_to_interval = concrete_to_interval

        super().__init__(
            model=converted_model,
            loss=loss,
            input_shape=input_shape,
            nb_classes=nb_classes,
            optimizer=converted_optimizer,
            channels_first=channels_first,
            clip_values=clip_values,
            preprocessing_defences=preprocessing_defences,
            postprocessing_defences=postprocessing_defences,
            preprocessing=preprocessing,
            device_type=device_type,
        )

    def predict_interval(  # pylint: disable=W0613
        self, x: np.ndarray, bounds: float, batch_size: int = 128, training_mode: bool = True, **kwargs
    ) -> Tuple[List[np.ndarray], List[np.ndarray]]:
        """

        :param x: The datapoint, representing the zonotope center.
        :param bounds: The perturbation range for the zonotope.
        :param batch_size: ...
        :param training_mode: `True` for model set to training mode and `'False` for model set to evaluation mode.
        :param kwargs: Dictionary of framework-specific arguments. This parameter is not currently supported for PyTorch
               and providing it takes no effect.
        """
        x_preprocessed, _ = self._apply_preprocessing(x, y=None, fit=False)
        self._model.train(mode=training_mode)
        if self.concrete_to_interval is None:
            x_interval = convert_to_interval(x=x_preprocessed, bounds=bounds, limits=[0, 1])
        else:
            x_interval = self.concrete_to_interval(x, bounds)

        num_batches = int(len(x_interval) / batch_size)
        interval_predictions = []
        # x_interval = torch.from_numpy(x_interval)

        for bnum in range(num_batches):
            x_batch_interval = x_interval[batch_size * bnum:batch_size * (bnum + 1)] # .to(self.device)
            abstract_preds = self.model.forward(x_batch_interval)
            interval_predictions.append(abstract_preds)

        x_batch_interval = x_interval[batch_size * num_batches:] # .to(self.device)
        abstract_preds = self.model.forward(x_batch_interval)
        interval_predictions.append(abstract_preds)

        interval_predictions = torch.concat(interval_predictions, dim=0)
        return interval_predictions.cpu().detach().numpy()

    def certify(self, cent: np.ndarray, eps: np.ndarray, prediction: int) -> bool:
        """
        Check if the datapoint has been certifiably classified.

        First do the forward pass through the NN with the given error terms and zonotope center to
        obtain the output zonotope.

        Then perform the certification step by computing the difference of the logits in the final zonotope
        and projecting to interval.

        :param cent: The datapoint, representing the zonotope center.
        :param eps: Error terms of the zonotope.
        :param prediction: The prediction the neural network gave on the basic datapoint.

        :return: True/False if the datapoint could be misclassified given the eps bounds.
        """
        cent_tensor, eps_tensor = self.model.forward(eps=eps, cent=cent)
        cent = cent_tensor.detach().cpu().numpy()
        eps = eps_tensor.detach().cpu().numpy()

        certification_results = []
        for k in range(self.nb_classes):
            if k != prediction:
                cert_via_sub = self.certify_via_subtraction(
                    predicted_class=prediction, class_to_consider=k, cent=cent, eps=eps
                )
                certification_results.append(cert_via_sub)

        return all(certification_results)

    def concrete_loss(self, output: "torch.Tensor", target: "torch.Tensor") -> "torch.Tensor":
        """
        Access function to get the classifier loss

        :param output: model predictions
        :param target: ground truth labels

        :return: loss value
        """
        return self._loss(output, target)

    def apply_preprocessing(self, x: np.ndarray, y: np.ndarray, fit: bool) -> Tuple[Any, Any]:
        """
        Access function to get preprocessing

        :param x: unprocessed input data.
        :param y: unprocessed labels.
        :param fit: `True` if the function is call before fit/training and `False` if the function is called before a
                     predict operation.
        :return: Tuple with the processed input data and labels.
        """
        x_preprocessed, y_preprocessed = self._apply_preprocessing(x, y, fit=fit)
        return x_preprocessed, y_preprocessed

    def max_logit_loss(self, prediction: "torch.Tensor", target: "torch.Tensor") -> Union["torch.Tensor", None]:
        """
        Computes the loss as the largest logit value amongst the incorrect classes.

        :param prediction: model predictions.
        :param target: target classes. NB not one hot.
        :return: scalar loss value
        """
        target_logit = prediction[:, target]
        output = prediction - target_logit

        ubs = torch.sum(torch.abs(output[1:, :]), dim=0) + output[0, :]

        loss = None
        for i in range(self.nb_classes):
            if i != target and (loss is None or ubs[i] > loss):
                loss = ubs[i]
        return loss

    @staticmethod
    def interval_loss_cce(prediction: "torch.Tensor", target: "torch.Tensor") -> "torch.Tensor":
        """
        Computes the categorical cross entropy loss with the correct class having the lower bound prediction,
        and the other classes having their upper bound predictions.

        :param prediction: model predictions.
        :param target: target classes. NB not one hot.
        :return: scalar loss value
        """
        criterion = torch.nn.CrossEntropyLoss()
        ubs = torch.sum(torch.abs(prediction[1:, :]), dim=0) + prediction[0, :]
        lbs = torch.sum(-1 * torch.abs(prediction[1:, :]), dim=0) + prediction[0, :]

        # for the prediction corresponding to the target class, take the lower bound predictions
        ubs[target] = lbs[target]
        ubs = torch.unsqueeze(ubs, dim=0)
        return criterion(ubs, target)

    @staticmethod
    def get_accuracy(preds: Union[np.ndarray, "torch.Tensor"], labels: Union[np.ndarray, "torch.Tensor"]) -> np.ndarray:
        """
        Helper function to print out the accuracy during training

        :param preds: (concrete) model predictions
        :param labels: ground truth labels (not one hot)
        :return: prediction accuracy
        """
        if isinstance(preds, torch.Tensor):
            preds = preds.detach().cpu().numpy()

        if isinstance(labels, torch.Tensor):
            labels = labels.detach().cpu().numpy()

        return np.sum(np.argmax(preds, axis=1) == labels) / len(labels)
