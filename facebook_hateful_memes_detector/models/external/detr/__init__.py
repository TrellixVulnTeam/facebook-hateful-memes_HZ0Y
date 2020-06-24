from pprint import pprint
from typing import Optional, List

import torch
from torch import nn
from torchvision.models import resnet50
import torchvision.transforms as T
from PIL import Image
import requests
import numpy as np
import random
# from ....utils import persistent_caching_fn


class DETRTransferBase(nn.Module):
    def __init__(self, model_name,  device, im_size=360,
                 num_tokens_out=64):
        super().__init__()
        self.to_tensor = T.Compose([
            T.Resize(im_size),
            T.ToTensor(),
            T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
        ])
        self.num_tokens_out = num_tokens_out
        self.model_name = model_name
        self.device = device

    @classmethod
    def set_seeds(cls, seed=0):
        torch.manual_seed(seed)
        np.random.seed(seed)
        random.seed(seed)

    @classmethod
    def read_image(cls, image_path):
        if "PIL" in str(type(image_path)):
            return image_path.convert('RGB')
        elif type(image_path) == str:
            if image_path.startswith('http'):
                image_path = requests.get(image_path, stream=True).raw
            return Image.open(image_path).convert('RGB')
        else:
            raise NotImplementedError

    def get_pil_image(self, image_path):
        if type(image_path) == torch.Tensor:
            assert len(image_path.size()) == 3
            return image_path.unsqueeze(0).to(self.device)
        elif "PIL" in str(type(image_path)) or type(image_path) == str:
            return self.to_tensor(self.read_image(image_path)).unsqueeze(0).to(self.device)
        else:
            raise NotImplementedError()

    def post_process(self, h, pred_boxes, pred_logits, pred_masks=None):
        out = torch.cat([h, pred_boxes, pred_logits], 2)
        probas = pred_logits[0, :, :-1]
        sorted_scores, sorted_indices = torch.sort(probas.max(-1).values, descending=True)
        sorted_scores, sorted_indices = sorted_scores[: self.num_tokens_out], sorted_indices[: self.num_tokens_out]

        return {'pred_logits': pred_logits[:, sorted_indices], 'h': h[:, sorted_indices], 'pred_boxes': pred_boxes[:, sorted_indices],
                "seq": out[:, sorted_indices], "pred_masks": pred_masks[:, sorted_indices] if pred_masks is not None else None}

    @classmethod
    def box_cxcywh_to_xyxy(cls, x):
        x_c, y_c, w, h = x.unbind(1)
        b = [(x_c - 0.5 * w), (y_c - 0.5 * h),
             (x_c + 0.5 * w), (y_c + 0.5 * h)]
        return torch.stack(b, dim=1)

    @classmethod
    def rescale_bboxes(cls, out_bbox, size):
        img_w, img_h = size
        b = cls.box_cxcywh_to_xyxy(out_bbox)
        b = b * torch.tensor([img_w, img_h, img_w, img_h], dtype=torch.float32)
        return b

    def plot_objects(self, image, n_objects=8, conf=0.7):
        image = self.read_image(image)
        outputs = self.forward(image)

        pred_logits = outputs['pred_logits'][:, :n_objects]
        pred_boxes = outputs['pred_boxes'][:, :n_objects]

        probas = pred_logits[0, :, :-1]
        keep = probas.max(-1).values > conf
        boxes = self.rescale_bboxes(pred_boxes[0, keep], image.size)
        prob = probas[keep]

        # COCO classes
        CLASSES = [
            'N/A', 'person', 'bicycle', 'car', 'motorcycle', 'airplane', 'bus',
            'train', 'truck', 'boat', 'traffic light', 'fire hydrant', 'N/A',
            'stop sign', 'parking meter', 'bench', 'bird', 'cat', 'dog', 'horse',
            'sheep', 'cow', 'elephant', 'bear', 'zebra', 'giraffe', 'N/A', 'backpack',
            'umbrella', 'N/A', 'N/A', 'handbag', 'tie', 'suitcase', 'frisbee', 'skis',
            'snowboard', 'sports ball', 'kite', 'baseball bat', 'baseball glove',
            'skateboard', 'surfboard', 'tennis racket', 'bottle', 'N/A', 'wine glass',
            'cup', 'fork', 'knife', 'spoon', 'bowl', 'banana', 'apple', 'sandwich',
            'orange', 'broccoli', 'carrot', 'hot dog', 'pizza', 'donut', 'cake',
            'chair', 'couch', 'potted plant', 'bed', 'N/A', 'dining table', 'N/A',
            'N/A', 'toilet', 'N/A', 'tv', 'laptop', 'mouse', 'remote', 'keyboard',
            'cell phone', 'microwave', 'oven', 'toaster', 'sink', 'refrigerator', 'N/A',
            'book', 'clock', 'vase', 'scissors', 'teddy bear', 'hair drier',
            'toothbrush'
        ]

        # colors for visualization
        COLORS = [[0.000, 0.447, 0.741], [0.850, 0.325, 0.098], [0.929, 0.694, 0.125],
                  [0.494, 0.184, 0.556], [0.466, 0.674, 0.188], [0.301, 0.745, 0.933]]

        import matplotlib.pyplot as plt
        plt.figure(figsize=(16, 10))
        plt.imshow(image)
        ax = plt.gca()
        for p, (xmin, ymin, xmax, ymax), c in zip(prob, boxes.tolist(), COLORS * 100):
            ax.add_patch(plt.Rectangle((xmin, ymin), xmax - xmin, ymax - ymin,
                                       fill=False, color=c, linewidth=3))
            cl = p.argmax()
            text = f'{CLASSES[cl]}: {p[cl]:0.2f}'
            ax.text(xmin, ymin, text, fontsize=15,
                    bbox=dict(facecolor='yellow', alpha=0.5))
        plt.axis('off')
        plt.show()

    def plot_panoptic(self, image, conf=0.85):
        assert self.enable_panoptic_plot
        image = self.read_image(image)
        out = self.forward(image)

        scores = out["pred_logits"][..., :-1].max(-1)[0]
        # threshold the confidence
        keep = scores > conf
        import matplotlib.pyplot as plt
        import math
        # Plot all the remaining masks
        ncols = 4
        fig, axs = plt.subplots(ncols=ncols, nrows=math.ceil(keep.sum().item() / ncols), figsize=(18, 10))
        for line in axs:
            for a in line:
                a.axis('off')
        for i, mask in enumerate(out["pred_masks"][keep]):
            ax = axs[i // ncols, i % ncols]
            ax.imshow(mask, cmap="cividis")
            ax.axis('off')
        fig.tight_layout()
        plt.show()

        result = self.postprocessor(out, torch.as_tensor(np.array(image).shape[-2:]).unsqueeze(0))[0]

        import panopticapi
        from panopticapi.utils import id2rgb, rgb2id
        import io

        import itertools
        import seaborn as sns
        palette = itertools.cycle(sns.color_palette())

        # The segmentation is stored in a special-format png
        panoptic_seg = Image.open(io.BytesIO(result['png_string']))
        panoptic_seg = np.array(panoptic_seg, dtype=np.uint8).copy()
        # We retrieve the ids corresponding to each mask
        panoptic_seg_id = rgb2id(panoptic_seg)

        # Finally we color each mask individually
        panoptic_seg[:, :, :] = 0
        for id in range(panoptic_seg_id.max() + 1):
            panoptic_seg[panoptic_seg_id == id] = np.asarray(next(palette)) * 255
        plt.figure(figsize=(15, 15))
        plt.imshow(panoptic_seg)
        plt.axis('off')
        plt.show()


        from detectron2.config import get_cfg
        from detectron2.utils.visualizer import Visualizer
        from detectron2.data import MetadataCatalog

        import cv2

        from copy import deepcopy
        # We extract the segments info and the panoptic result from DETR's prediction
        segments_info = deepcopy(result["segments_info"])
        # Panoptic predictions are stored in a special format png
        panoptic_seg = Image.open(io.BytesIO(result['png_string']))
        final_w, final_h = panoptic_seg.size
        # We convert the png into an segment id map
        panoptic_seg = np.array(panoptic_seg, dtype=np.uint8)
        panoptic_seg = torch.from_numpy(rgb2id(panoptic_seg))

        # Detectron2 uses a different numbering of coco classes, here we convert the class ids accordingly
        meta = MetadataCatalog.get("coco_2017_val_panoptic_separated")
        for i in range(len(segments_info)):
            c = segments_info[i]["category_id"]
            segments_info[i]["category_id"] = meta.thing_dataset_id_to_contiguous_id[c] if segments_info[i]["isthing"] else \
            meta.stuff_dataset_id_to_contiguous_id[c]

        # Finally we visualize the prediction
        v = Visualizer(np.array(image.copy().resize((final_w, final_h)))[:, :, ::-1], meta, scale=1.0)
        v._default_font_size = 20
        v = v.draw_panoptic_seg_predictions(panoptic_seg, segments_info, area_threshold=0)
        print(type(v), type(v.get_image()))
        # cv2.imshow("Panoptic Segment Image", v.get_image())
        plt.figure(figsize=(16, 10))
        plt.imshow(Image.fromarray(v.get_image()[:, :, ::-1]))
        plt.axis('off')
        plt.show()


    def show(self, image, **kwargs):
        if "panoptic" in self.model_name:
            self.plot_panoptic(image, **kwargs)
        else:
            self.plot_objects(image, **kwargs)


class DETRdemo(DETRTransferBase):
    """
    Demo DETR implementation.

    Demo implementation of DETR in minimal number of lines, with the
    following differences wrt DETR in the paper:
    * learned positional encoding (instead of sine)
    * positional encoding is passed at input (instead of attention)
    * fc bbox predictor (instead of MLP)
    The model achieves ~40 AP on COCO val5k and runs at ~28 FPS on Tesla V100.
    Only batch size 1 supported.

    https://colab.research.google.com/github/facebookresearch/detr/blob/colab/notebooks/detr_demo.ipynb

    Usage:
    detr = DETRdemo(device=torch.device('cuda'), num_tokens_out=16)
    detr.show_boxes('http://images.cocodataset.org/val2017/000000039769.jpg', 8, 0.5)
    """

    def __init__(self, device, im_size=360,
                 num_tokens_out=64):
        super().__init__("demo", device, im_size, num_tokens_out)
        self.num_tokens_out = num_tokens_out
        # create ResNet-50 backbone
        self.backbone = resnet50()
        del self.backbone.fc
        hidden_dim = 256
        nheads = 8
        num_encoder_layers = 6
        num_decoder_layers = 6
        self.conv = nn.Conv2d(2048, hidden_dim, 1)

        # create a default PyTorch transformer
        self.transformer = nn.Transformer(
            hidden_dim, nheads, num_encoder_layers, num_decoder_layers)
        self.linear_class = nn.Linear(hidden_dim, 91 + 1)
        self.linear_bbox = nn.Linear(hidden_dim, 4)
        self.query_pos = nn.Parameter(torch.rand(100, hidden_dim))
        self.row_embed = nn.Parameter(torch.rand(50, hidden_dim // 2))
        self.col_embed = nn.Parameter(torch.rand(50, hidden_dim // 2))

        state_dict = torch.hub.load_state_dict_from_url(
            url='https://dl.fbaipublicfiles.com/detr/detr_demo-da2a99e9.pth',
            map_location=device, check_hash=True)
        self.load_state_dict(state_dict)
        self.to(self.device)
        self.eval()
        for p in self.parameters():
            p.requires_grad = False

    def forward(self, inputs):
        # propagate inputs through ResNet-50 up to avg-pool layer
        self.set_seeds()
        inputs = self.get_pil_image(inputs)
        inputs = inputs.to(self.device)
        x = self.backbone.conv1(inputs)
        x = self.backbone.bn1(x)
        x = self.backbone.relu(x)
        x = self.backbone.maxpool(x)

        x = self.backbone.layer1(x)
        x = self.backbone.layer2(x)
        x = self.backbone.layer3(x)
        x = self.backbone.layer4(x)

        # convert from 2048 to 256 feature planes for the transformer
        h = self.conv(x)

        # construct positional encodings
        H, W = h.shape[-2:]
        pos = torch.cat([
            self.col_embed[:W].unsqueeze(0).repeat(H, 1, 1),
            self.row_embed[:H].unsqueeze(1).repeat(1, W, 1),
        ], dim=-1).flatten(0, 1).unsqueeze(1)

        # propagate through the transformer
        h = self.transformer(pos + 0.1 * h.flatten(2).permute(2, 0, 1),
                             self.query_pos.unsqueeze(1)).transpose(0, 1)

        pred_logits = self.linear_class(h).softmax(-1)
        pred_boxes = self.linear_bbox(h).sigmoid()
        return self.post_process(h, pred_boxes, pred_logits)

    def batch_forward(self, inputs):
        assert type(inputs) == list or type(inputs) == tuple or (type(inputs) == torch.Tensor and len(inputs.size()) == 4)


class NestedTensor(object):
    def __init__(self, tensors, mask: Optional[torch.Tensor]):
        self.tensors = tensors
        self.mask = mask

    def to(self, device):
        # type: (Device) -> NestedTensor # noqa
        cast_tensor = self.tensors.to(device)
        mask = self.mask
        if mask is not None:
            assert mask is not None
            cast_mask = mask.to(device)
        else:
            cast_mask = None
        return NestedTensor(cast_tensor, cast_mask)

    def decompose(self):
        return self.tensors, self.mask

    def __repr__(self):
        return str(self.tensors)


def _max_by_axis(the_list):
    # type: (List[List[int]]) -> List[int]
    maxes = the_list[0]
    for sublist in the_list[1:]:
        for index, item in enumerate(sublist):
            maxes[index] = max(maxes[index], item)
    return maxes


def nested_tensor_from_tensor_list(tensor_list: List[torch.Tensor]):
    if tensor_list[0].ndim == 3:
        # TODO make it support different-sized images
        max_size = _max_by_axis([list(img.shape) for img in tensor_list])
        # min_size = tuple(min(s) for s in zip(*[img.shape for img in tensor_list]))
        batch_shape = [len(tensor_list)] + max_size
        b, c, h, w = batch_shape
        dtype = tensor_list[0].dtype
        device = tensor_list[0].device
        tensor = torch.zeros(batch_shape, dtype=dtype, device=device)
        mask = torch.ones((b, h, w), dtype=torch.bool, device=device)
        for img, pad_img, m in zip(tensor_list, tensor, mask):
            pad_img[: img.shape[0], : img.shape[1], : img.shape[2]].copy_(img)
            m[: img.shape[1], :img.shape[2]] = False
    else:
        raise ValueError('not supported')
    return NestedTensor(tensor, mask)


class DETR(DETRTransferBase):
    def __init__(self, device: torch.device, resnet='resnet50',
                 decoder_layer=-3, im_size=360,
                 num_tokens_out=64,
                 enable_panoptic_plot=True):
        assert num_tokens_out < 400 and num_tokens_out % 4 == 0
        super().__init__(resnet, device, im_size, num_tokens_out)
        self.device = device
        if "panoptic" in resnet:
            model, postprocessor = torch.hub.load('facebookresearch/detr', 'detr_%s' % resnet, pretrained=True, return_postprocessor=True,
                                                  num_classes=250)
            self.model = model
            self.postprocessor = postprocessor
        else:
            self.model = torch.hub.load('facebookresearch/detr', 'detr_%s' % resnet, pretrained=True)
        self.model.to(device)
        # model, postprocessor = torch.hub.load('facebookresearch/detr', 'detr_resnet101_panoptic', pretrained=True, return_postprocessor=True, num_classes=250)
        n_parameters = sum(p.numel() for p in self.model.parameters() if p.requires_grad)
        print('number of params:', n_parameters)
        for p in self.model.parameters():
            p.requires_grad = False
        self.decoder_layer = decoder_layer
        self.enable_panoptic_plot = enable_panoptic_plot

    def forward(self, images):
        samples = self.get_pil_image(images)
        samples = nested_tensor_from_tensor_list(samples)
        pred_masks = None
        self.set_seeds()
        if "panoptic" in self.model_name:
            features, pos = self.model.detr.backbone(samples)

            bs = features[-1].tensors.shape[0]

            src, mask = features[-1].decompose()
            assert mask is not None
            src_proj = self.model.detr.input_proj(src)
            hs, enc_repr = self.model.detr.transformer(src_proj, mask, self.model.detr.query_embed.weight, pos[-1])

            outputs_class = self.model.detr.class_embed(hs[-1]).softmax(-1)
            outputs_coord = self.model.detr.bbox_embed(hs[-1]).sigmoid()
            h, pred_boxes, pred_logits = hs[self.decoder_layer], outputs_coord, outputs_class
            if self.enable_panoptic_plot:
                bbox_mask = self.model.bbox_attention(hs[-1], enc_repr, mask=mask)
                seg_masks = self.model.mask_head(src_proj, bbox_mask, [features[2].tensors, features[1].tensors, features[0].tensors])
                outputs_seg_masks = seg_masks.view(bs, self.model.detr.num_queries, seg_masks.shape[-2], seg_masks.shape[-1])
                pred_masks = outputs_seg_masks
                return {'pred_logits': pred_logits, 'pred_boxes': pred_boxes,
                        "pred_masks": pred_masks}


        else:
            samples.to(self.device)
            features, pos = self.model.backbone(samples)

            src, mask = features[-1].decompose()
            assert mask is not None
            hs, enc_repr = self.model.transformer(self.model.input_proj(src), mask, self.model.query_embed.weight, pos[-1])
            hs = hs[self.decoder_layer]
            outputs_class = self.model.class_embed(hs).softmax(-1)
            outputs_coord = self.model.bbox_embed(hs).sigmoid()

            h, pred_boxes, pred_logits = hs, outputs_coord, outputs_class
        enc_repr = enc_repr.flatten(2, 3).transpose(1, 2)
        outs = self.post_process(h, pred_boxes, pred_logits, pred_masks)
        return outs


class DETRPanoptic(nn.Module):
    pass


if __name__ == "__main__":
    detr = DETR(torch.device('cpu'), 'resnet101_panoptic', decoder_layer=-3, im_size=640, num_tokens_out=100)
    import time
    s = time.time()
    image_url = "http://images.cocodataset.org/val2017/000000281759.jpg"
    # "http://images.cocodataset.org/val2017/000000281759.jpg"
    # 'http://images.cocodataset.org/val2017/000000039769.jpg'
    detr.show(image_url)
    e = time.time() - s
    print("Time Taken For DETR = %.4f" % e)

    s = time.time()
    detrd = DETRdemo(torch.device('cpu'))
    detrd.show(image_url)
    e = time.time() - s
    print("Time Taken For DETR Demo = %.4f" % e)

