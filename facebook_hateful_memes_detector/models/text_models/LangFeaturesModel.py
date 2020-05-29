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
from flair.data import Sentence
from flair.embeddings import FlairEmbeddings, BytePairEmbeddings, CharacterEmbeddings, WordEmbeddings, TransformerWordEmbeddings, StackedEmbeddings
from flair.models import SequenceTagger
import json
import csv
import numpy as np

from torchmoji.sentence_tokenizer import SentenceTokenizer
from torchmoji.model_def import torchmoji_emojis
from torchmoji.global_variables import PRETRAINED_PATH, VOCAB_PATH
import stanza


import spacy

from ...utils import init_fc, GaussianNoise, stack_and_pad_tensors, get_pos_tag_indices, pad_tensor, \
    get_penn_treebank_pos_tag_indices, get_all_tags
from ...utils import get_universal_deps_indices
from .FasttextPooled import FasttextPooledModel
from ..ibm_max import ModelWrapper


class LangFeaturesModel(FasttextPooledModel):
    def __init__(self, classifer_dims, num_classes,
                 gaussian_noise=0.0, dropout=0.0, use_as_submodel=False, use_as_super=False,
                 **kwargs):
        super(LangFeaturesModel, self).__init__(classifer_dims, num_classes, gaussian_noise, dropout, use_as_submodel, True, **kwargs)
        extrafeats = kwargs["extrafeats"] if "extrafeats" in kwargs else False
        self.extrafeats = extrafeats
        gru_layers = kwargs["gru_layers"] if "gru_layers" in kwargs else 2
        gru_dropout = kwargs["gru_dropout"] if "gru_dropout" in kwargs else 0.1

        self.nlp = spacy.load("en_core_web_lg", disable=[])
        self.snlp = stanza.Pipeline('en', processors='tokenize,pos,lemma,depparse,ner', use_gpu=True,
                                    pos_batch_size=3000)
        self.pdict = get_all_tags()
        embedding_dim = 8

        self.tag_em = nn.Embedding(len(self.pdict)+1, embedding_dim)
        # nn.init.normal_(self.embeds.weight, std=1 / embedding_dim)
        init_fc(self.tag_em, "linear")

        self.sw_em = nn.Embedding(2, embedding_dim)
        nn.init.normal_(self.sw_em.weight, std=1 / embedding_dim)

        self.w_len = nn.Embedding(16, embedding_dim)
        nn.init.normal_(self.w_len.weight, std=1 / embedding_dim)

        self.wc_emb = nn.Embedding(8, embedding_dim)
        nn.init.normal_(self.wc_emb.weight, std=1 / embedding_dim)

        gru_dims = kwargs["gru_dims"] if "gru_dims" in kwargs else int(classifer_dims / 2)
        if not use_as_super:
            lin1 = nn.Linear(gru_dims * 2, gru_dims * 4)
            init_fc(lin1, "leaky_relu")
            lin2 = nn.Linear(gru_dims * 4, classifer_dims)
            init_fc(lin2, "linear")
            self.projection = nn.Sequential(nn.Dropout(dropout), lin1, nn.LeakyReLU(), lin2)

            self.lstm = nn.Sequential(
                nn.GRU(166, gru_dims, gru_layers, batch_first=True, bidirectional=True, dropout=gru_dropout))

        if self.extrafeats:
            self.ibm_max = ModelWrapper()
            with open(VOCAB_PATH, 'r') as f:
                maxlen = 64
                self.vocabulary = json.load(f)
                self.st = SentenceTokenizer(self.vocabulary, maxlen)
                self.tmoji = torchmoji_emojis(PRETRAINED_PATH)

    def get_torchmoji_probas(self,  texts: List[str]):
        tokenized, _, _ = self.st.tokenize_sentences(texts)
        prob = self.tmoji(tokenized)
        return torch.tensor(prob)

    def get_word_and_text_lengths(self):
        pass

    def get_word_vectors(self, texts: List[str]):
        pdict = self.pdict
        nlp = self.nlp
        snlp = self.snlp
        docs = [list(map(lambda x:dict(**x.to_dict()[0], ner=x.ner), snlp(doc).iter_tokens())) for doc in texts]

        upos = stack_and_pad_tensors(list(map(lambda x: torch.tensor([pdict[token["upos"].lower()] for token in x]), docs)), 64)
        upos_emb = self.tag_em(upos)

        xpos = stack_and_pad_tensors(list(map(lambda x: torch.tensor([pdict[token["xpos"].lower()] for token in x]), docs)), 64)
        xpos_emb = self.tag_em(xpos)

        deprel = stack_and_pad_tensors(list(map(lambda x: torch.tensor([pdict[token["deprel"].split(":")[0].lower()] for token in x]), docs)),
                                     64)
        deprel_emb = self.tag_em(deprel)

        deprel2 = stack_and_pad_tensors(
            list(map(lambda x: torch.tensor([pdict[token["deprel"].split(":")[1].lower()] if ":" in token["deprel"] else 0 for token in x]), docs)),
            64)
        deprel_emb2 = self.tag_em(deprel2)

        sner = stack_and_pad_tensors(
            list(map(lambda x: torch.tensor([pdict[token["ner"].split("-")[1].lower()] if "-" in token["ner"] else 0 for token in x]), docs)),
            64)
        sner_emb = self.tag_em(sner)

        head = stack_and_pad_tensors(
            list(map(lambda x: torch.tensor([token["head"] / 100.0 for token in x]), docs)),
            64)



        spacy_texts = list(nlp.pipe(texts, n_process=4))
        wl = stack_and_pad_tensors(
            list(map(lambda x: torch.tensor([len(token) - 1 for token in x]).clamp(0, 15), spacy_texts)), 64)
        wl_emb = self.w_len(wl)
        wc = (torch.tensor(list(map(len, spacy_texts)))/10).long().unsqueeze(1).expand(len(texts), 64)
        wc_emb = self.wc_emb(wc)

        pos = stack_and_pad_tensors(list(map(lambda x: torch.tensor([pdict[token.pos_.lower()] for token in x]), spacy_texts)), 64)
        pos_emb = self.tag_em(pos)
        tag = stack_and_pad_tensors(list(map(lambda x: torch.tensor([pdict[token.tag_.lower()] for token in x]), spacy_texts)), 64)
        tag_emb = self.tag_em(tag)
        dep = stack_and_pad_tensors(list(map(lambda x: torch.tensor([pdict[token.dep_.lower()] for token in x]), spacy_texts)), 64)
        dep_emb = self.tag_em(dep)
        sw = stack_and_pad_tensors(list(map(lambda x: torch.tensor([int(token.is_stop) for token in x]), spacy_texts)), 64)
        sw_emb = self.sw_em(sw)
        ner = stack_and_pad_tensors(
            list(map(lambda x: torch.tensor([pdict[token.ent_type_.lower()] for token in x]), spacy_texts)), 64)
        ner_emb = self.tag_em(ner)
        result = torch.cat([pos_emb, tag_emb, dep_emb, sw_emb, ner_emb, upos_emb, xpos_emb, deprel_emb, sner_emb, deprel_emb2, wl_emb, wc_emb], 2)

        if self.extrafeats:
            tm_probas = self.get_torchmoji_probas(texts)
            ibm_max = self.ibm_max.predict(texts)
            tm_probas = tm_probas.unsqueeze(1).expand(len(texts), 64, tm_probas.size(1))
            ibm_max = ibm_max.unsqueeze(1).expand(len(texts), 64, ibm_max.size(1))
            result = torch.cat([result, tm_probas, ibm_max], 2)

        result = result / result.norm(dim=2, keepdim=True).clamp(min=1e-5)  # Normalize in word dimension
        return result

    def __get_scores__(self, texts: List[str], img=None):
        vectors = self.get_word_vectors(texts)
        lstm_output, _ = self.lstm(vectors)
        lstm_output = self.projection(lstm_output)
        mean_projection = lstm_output.mean(1)
        # mean_projection = torch.cat([mean_projection, tm_probas], 1)
        return mean_projection, lstm_output
