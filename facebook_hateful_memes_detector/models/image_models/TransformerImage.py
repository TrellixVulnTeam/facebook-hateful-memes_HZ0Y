import abc
from typing import List, Tuple, Dict, Set, Union
import numpy as np
import torch.nn as nn
import torch
import torch.nn.functional as F
from mmf.common import SampleList

from ..classifiers import CNN1DFeaturizer, GRUFeaturizer, BasicFeaturizer, TransformerFeaturizer
from ..text_models import AlbertClassifer
from transformers import AutoModelWithLMHead, AutoTokenizer, AutoModel, LongformerTokenizer, LongformerModel, DistilBertTokenizer, DistilBertModel
from transformers import AlbertModel, AlbertTokenizer, AlbertForSequenceClassification
import torchvision.models as models
from torchnlp.word_to_vector import CharNGram
from torchnlp.word_to_vector import BPEmb
from ...utils import get_device, GaussianNoise, random_word_mask, load_stored_params, ExpandContract, Transformer, PositionalEncoding, LambdaLayer, get_global, \
    get_torchvision_classification_models, get_image_info_fn, LambdaLayer, get_vgg_face_model, PositionalEncoding2D, Transpose, init_fc, dict2sampleList, \
    clean_memory
from ..external.detr import get_detr_model
import transformers
import os
import random
import math


class TransformerImageModel(AlbertClassifer):
    def __init__(self, image_models, classifier_dims, num_classes,
                 gaussian_noise, dropout,
                 internal_dims, n_layers,
                 featurizer, final_layer_builder,
                 n_tokens_in=64, n_tokens_out=16,
                 head_masks=0,
                 use_as_super=False, **kwargs):
        embedding_dims = 768
        super(TransformerImageModel, self).__init__(classifier_dims, num_classes, gaussian_noise, dropout,
                                                    internal_dims, n_layers,
                                                    featurizer, final_layer_builder,
                                                    n_tokens_in, n_tokens_out, True, **kwargs)
        assert n_tokens_in % n_tokens_out == 0
        #
        self.head_masks = head_masks
        assert self.head_masks <= 12

        names, im_models, im_shapes, im_procs = [], [], [], []
        for imo in image_models:
            if type(imo) == dict:
                module_gaussian = imo["gaussian_noise"] if "gaussian_noise" in imo else 0.0
                module_dropout = imo["dropout"] if "dropout" in imo else 0.0
                large_rf = imo["large_rf"] if "large_rf" in imo else True
                imo = imo["model"]
            elif type(imo) == str:
                module_gaussian = 0.0
                module_dropout = 0.0
                large_rf = True
            else:
                raise NotImplementedError()

            if "torchvision" in imo:
                net = "_".join(imo.split("_")[1:])
                im_model, im_shape = get_torchvision_classification_models(net, large_rf)
                im_model = LambdaLayer(im_model, module_gaussian, module_dropout)
                self.torchvision_pool = nn.AdaptiveAvgPool2d(1)
                shape_global = im_shape[1]
                def global_view(x):
                    xv = self.torchvision_pool(x).expand(-1, -1, shape_global, -1)
                    return torch.cat([x, xv], 3)

                im_shape = list(im_shape)
                im_shape[-1] = im_shape[-1] + 1

                lin = nn.Linear(im_shape[0], embedding_dims)
                init_fc(lin, "leaky_relu")
                lin = nn.Sequential(lin, nn.LeakyReLU())
                im_proc = nn.Sequential(LambdaLayer(global_view), Transpose(1, 2), Transpose(2, 3), lin,
                                        LambdaLayer(lambda v: v * math.sqrt(embedding_dims)),
                                        PositionalEncoding2D(embedding_dims, dropout, channels_first=False), Transpose(0, 1))
                im_shape = (embedding_dims, im_shape[-1] * im_shape[-2])

            elif imo == "caption_features":
                im_model = LambdaLayer(get_image_info_fn(enable_encoder_feats=True)["get_batch_encoder_feats"], module_gaussian, module_dropout)
                im_shape = (512, 100)
                lin = nn.Linear(im_shape[0], embedding_dims)
                init_fc(lin, "leaky_relu")
                lin = nn.Sequential(lin, nn.LeakyReLU())
                im_proc = lin
            elif "detr" in imo:
                im_shape = (256, 100)
                im_model = LambdaLayer(get_detr_model(get_device(), imo)["batch_detr_fn"], module_gaussian, module_dropout)
                lin = nn.Linear(im_shape[0], embedding_dims)
                init_fc(lin, "leaky_relu")
                lin = nn.Sequential(lin, nn.LeakyReLU())
                im_proc = lin
            elif "vgg_face" in imo:
                im_shape = (256, 1)
                im_model = LambdaLayer(get_vgg_face_model(), module_gaussian, module_dropout)
                lin = nn.Linear(im_shape[0], embedding_dims)
                init_fc(lin, "leaky_relu")
                lin = nn.Sequential(lin, nn.LeakyReLU())
                im_proc = lin
            else:
                raise NotImplementedError(imo)

            names.append(imo)
            im_models.append(im_model)
            im_shapes.append(im_shape)
            im_procs.append(im_proc)
        self.im_models = nn.ModuleDict(dict(zip(names, im_models)))
        self.post_procs = nn.ModuleDict(dict(zip(names, im_procs)))
        self.im_shapes = dict(zip(names, im_shapes))
        self.require_raw_img = {"detr_demo", 'detr_resnet50', 'detr_resnet50_panoptic', 'detr_resnet101', 'detr_resnet101_panoptic',
                                "ssd", "faster_rcnn", "lxmert_faster_rcnn", "caption_features"}

        self.total_tokens = n_tokens_in + 1 + ((8 * int(self.n_tokens_in/(8*1.375) + 1)) if self.need_fasttext else 0) + sum([s[-1] for s in im_shapes])
        self.text_tokens = n_tokens_in

        if not use_as_super:
            model = kwargs["model"] if "model" in kwargs else 'albert-base-v2'
            model_class = AutoModel
            tokenizer_class = AutoTokenizer
            if "distilbert" in model:
                model_class = DistilBertModel
                tokenizer_class = DistilBertTokenizer
                tokenizer = "distilbert-base-uncased"
            elif "longformer" in model:
                model_class = LongformerModel
                tokenizer = "allenai/longformer-base-4096"
                tokenizer_class = LongformerTokenizer
            elif "albert" in model:
                model_class = AlbertModel
                tokenizer_class = AlbertTokenizer
                tokenizer = "albert-base-v2"
            else:
                raise NotImplementedError

            global_dir = get_global("models_dir")
            model = os.path.join(global_dir, model) if model in os.listdir(global_dir) else model
            self.tokenizer = tokenizer_class.from_pretrained(tokenizer)
            self.model = model_class.from_pretrained(model)
            print("Pick stored Model", model, "Model Class = ", type(self.model), "Tokenizer Class = ", type(self.tokenizer))
            if featurizer == "transformer":
                n_encoders = kwargs.pop("n_encoders", n_layers)
                n_decoders = kwargs.pop("n_decoders", n_layers)
                self.featurizer = TransformerFeaturizer(self.total_tokens, embedding_dims, n_tokens_out,
                                                        classifier_dims,
                                                        internal_dims, n_encoders, n_decoders, gaussian_noise, dropout)
            else:
                raise NotImplementedError()

            self.final_layer = final_layer_builder(classifier_dims, n_tokens_out, num_classes, dropout, **kwargs)

        self.LayerNorm = nn.LayerNorm(embedding_dims, eps=1e-12)
        self.dropout = nn.Dropout(dropout)
        if "stored_model" in kwargs:
            load_stored_params(self, kwargs["stored_model"])

        self.reg_layers = [(c, c.p if hasattr(c, "p") else c.sigma) for c in self.children() if c.__class__ == GaussianNoise or c.__class__ == nn.Dropout]

    def tokenise(self, texts: List[str]):
        tokenizer = self.tokenizer
        n_tokens_in = self.text_tokens
        if self.training and self.word_masking_proba > 0:
            texts = [random_word_mask(t, tokenizer, self.word_masking_proba) for t in texts]
        converted_texts = tokenizer.batch_encode_plus(texts, add_special_tokens=True, pad_to_max_length=True, max_length=n_tokens_in, truncation=True)
        input_ids, attention_mask = converted_texts["input_ids"], converted_texts["attention_mask"]
        return torch.tensor(input_ids).to(get_device()), torch.tensor(attention_mask).to(get_device())

    def get_vectors(self, sampleList: SampleList):
        sampleList = dict2sampleList(sampleList, device=get_device())
        img = sampleList.torchvision_image
        image = sampleList.image
        input_ids, attention_mask = self.tokenise(sampleList.text)
        word_embeddings = self.model.embeddings(input_ids) # B, S, C
        image_vectors = list()
        if len(set(self.im_models.keys()) - self.require_raw_img) > 0:
            img = img.to(get_device())

        if self.need_fasttext:
            fasttext_vectors = self.fasttext_vectors(sampleList.text)
            seq_length = word_embeddings.size(1)
            position_ids = torch.arange(seq_length + 1, seq_length + fasttext_vectors.size(1), dtype=torch.long, device=input_ids.device)  # (max_seq_length)
            position_ids = position_ids.unsqueeze(0).expand_as(input_ids)  # (bs, max_seq_length)
            position_embeddings = self.model.embeddings.position_embeddings(position_ids)  # (bs, max_seq_length, dim)
            fasttext_vectors = fasttext_vectors + position_embeddings  # (bs, max_seq_length, dim)
            fasttext_vectors = self.LayerNorm(fasttext_vectors)  # (bs, max_seq_length, dim)
            fasttext_vectors = self.dropout(fasttext_vectors)  # (bs, max_seq_length, dim)
            attention_mask = torch.cat([attention_mask, torch.ones(attention_mask.size(0), fasttext_vectors.size(1))], 1)
            word_embeddings = torch.cat([word_embeddings, fasttext_vectors], 1)

        for k, m in self.im_models.items():
            im_repr = m(image if k in self.require_raw_img else img)
            im_repr = self.post_procs[k](im_repr)
            image_vectors.append(im_repr.to(get_device()))
            clean_memory()

        image_vectors = torch.cat(image_vectors, 1)
        seq_length = word_embeddings.size(1)
        position_ids = torch.arange(seq_length, seq_length + image_vectors.size(1), dtype=torch.long, device=input_ids.device)  # (max_seq_length)
        position_ids = position_ids.unsqueeze(0).expand(image_vectors.size()[:2])  # (bs, max_seq_length)
        position_embeddings = self.model.embeddings.position_embeddings(position_ids)  # (bs, max_seq_length, dim)

        image_vectors = image_vectors + position_embeddings  # (bs, max_seq_length, dim)
        image_vectors = self.LayerNorm(image_vectors)  # (bs, max_seq_length, dim)
        image_vectors = self.dropout(image_vectors)  # (bs, max_seq_length, dim)
        attention_mask = attention_mask.to(get_device())
        image_vectors = image_vectors.to(get_device())
        attention_mask = torch.cat([attention_mask, torch.ones(attention_mask.size(0), image_vectors.size(1), device=get_device(), dtype=attention_mask.dtype)], 1)
        embeddings = torch.cat([word_embeddings, image_vectors], 1)

        if self.training:
            head_mask = [1] * (12 - self.head_masks) + [0] * self.head_masks
            random.shuffle(head_mask)
        else:
            head_mask = [1] * 12
        encoder = getattr(self.model, "transformer", getattr(self.model, "encoder", None))
        if type(self.model) == transformers.modeling_longformer.LongformerModel:
            attention_window = (
                self.model.config.attention_window
                if isinstance(self.model.config.attention_window, int)
                else max(self.model.config.attention_window)
            )
            padding_len, input_ids, attention_mask, token_type_ids, position_ids, embeddings = self.model._pad_to_window_size(
                input_ids=None,
                attention_mask=attention_mask,
                token_type_ids=None,
                position_ids=None,
                inputs_embeds=embeddings,
                attention_window=attention_window,
                pad_token_id=self.model.config.pad_token_id,
            )
            attention_mask = attention_mask.unsqueeze(2)
        tfmr_output = encoder(embeddings, attention_mask, head_mask=head_mask)
        hidden_state = tfmr_output[0]
        output = (hidden_state,) + tfmr_output[1:]
        return hidden_state

    def forward(self, sampleList: SampleList):
        sampleList = dict2sampleList(sampleList, device=get_device())
        labels = torch.tensor(sampleList.label).to(get_device())
        # sample_weights = torch.tensor(sampleList.sample_weight, dtype=float).to(get_device())
        vectors = self.get_vectors(sampleList)
        vectors = self.featurizer(vectors)
        logits, loss = self.final_layer(vectors, labels) if self.final_layer is not None else (None, None)

        if self.training:
            loss += self.auc_dice_loss(logits, labels)
        return logits, vectors.mean(1), vectors, loss