import torchvision
import torchvision.models as models

import abc
from abc import ABC
from typing import List, Tuple, Dict, Set, Union
import numpy as np
import torch.nn as nn
import torch
import torchnlp
import torch.nn.functional as F
import fasttext
from torchnlp.word_to_vector import CharNGram
from torchnlp.word_to_vector import BPEmb

from ...utils import init_fc, GaussianNoise, stack_and_pad_tensors, get_torchvision_classification_models
from ..classifiers import CNN1DFeaturizer, GRUFeaturizer, TransformerFeaturizer, TransformerEnsembleFeaturizer
from ..text_models import Fasttext1DCNNModel, LangFeaturesModel


class MultiImageMultiTextAttentionEarlyFusionModel(nn.Module):
    def __init__(self, image_models, num_classes,
                 text_models,
                 gaussian_noise, dropout,
                 internal_dims, classifier_dims,
                 final_layer_builder,
                 n_tokens_out, n_layers,
                 finetune_image_model=False,
                 **kwargs):
        super(MultiImageMultiTextAttentionEarlyFusionModel, self).__init__()
        assert type(image_models) == list
        im_models, im_shapes, im_procs = [], [], []
        for imo in image_models:
            if type(imo) == str:
                if "torchvision" in imo:
                    net = imo.split("_")[-1]
                    im_model, im_shape = get_torchvision_classification_models(net, finetune_image_model)

                else:
                    raise NotImplementedError(imo)

            else:
                raise NotImplementedError()
            imf_width = im_shape[-1]
            l1 = nn.Conv2d(im_shape[0], int(im_shape[0] / 2), 1, 1, padding=0, groups=4, bias=False)
            init_fc(l1, "leaky_relu")
            l2 = nn.Conv2d(int(im_shape[0] / 2), internal_dims, 3, 1, padding=1, groups=1, bias=False)
            init_fc(l2, "leaky_relu")
            im_proc = nn.Sequential(nn.Dropout(dropout), l1, nn.LeakyReLU(),
                                    GaussianNoise(gaussian_noise), l2, nn.LeakyReLU())

            im_shape = (internal_dims, im_shape[1], im_shape[2])
            im_models.append(im_model)
            im_shapes.append(im_shape)
            im_procs.append(im_proc)

        assert type(text_models) == list
        tx_models, names = [], []
        for i, tm in enumerate(text_models):
            assert type(tm) == dict
            text_model_class, text_model_params, text_in_channels, text_in_tokens = tm["cls"], tm["params"], tm["in_channels"], tm["in_tokens"]
            text_model = text_model_class(**text_model_params)
            tx_models.append(text_model)
            names.append("tx_" + str(i))

        self.im_models = nn.ModuleDict(dict(zip(image_models, im_models)))
        self.im_procs = nn.ModuleDict(dict(zip(image_models, im_procs)))
        self.tx_models = nn.ModuleDict(dict(zip(names, tx_models)))
        self.im_shapes = dict(zip(image_models, im_shapes))
        self.text_models = dict(zip(names, text_models))

        ensemble_conf = {k: dict(is2d=True, n_tokens_in=v[-1] * v[-1], n_channels_in=v[0]) for k, v in self.im_shapes.items()}
        ensemble_conf.update(
            {k: dict(is2d=False, n_tokens_in=v["in_tokens"], n_channels_in=v["in_channels"]) for k, v in
             self.text_models.items()})
        self.featurizer = TransformerEnsembleFeaturizer(ensemble_conf, n_tokens_out, classifier_dims, internal_dims,
                                                        n_layers, gaussian_noise, dropout)

        self.final_layer = final_layer_builder(classifier_dims, n_tokens_out, num_classes, dropout, )
        self.finetune_image_model = finetune_image_model

    def forward(self, texts: List[str], img, labels, sample_weights=None):
        vectors = dict()
        for k, m in self.tx_models.items():
            _, _, text_repr, _ = m(texts, img, labels, sample_weights)
            vectors[k] = text_repr

        for k, m in self.im_models.items():
            if self.finetune_image_model:
                im_repr = m(img)
            else:
                with torch.no_grad():
                    im_repr = m(img)
            im_repr = self.im_procs[k](im_repr)
            vectors[k] = im_repr

        vectors = self.featurizer(vectors)
        logits, loss = self.final_layer(vectors, labels)
        return logits, vectors.mean(1), vectors, loss
