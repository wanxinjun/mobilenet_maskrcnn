# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.
import torch
import torch.nn.functional as F
from torch import nn

from maskrcnn_benchmark.structures.bounding_box import BoxList
from maskrcnn_benchmark.structures.boxlist_ops import boxlist_nms
from maskrcnn_benchmark.structures.boxlist_ops import cat_boxlist
from maskrcnn_benchmark.modeling.box_coder import BoxCoder


class PostProcessor(nn.Module):  # 对应图中rpn post processor
    """
    From a set of classification scores, box regression and proposals,
    computes the post-processed boxes, and applies NMS to obtain the
    final results
    """

    def __init__(
            self,
            score_thresh=0.05,
            nms=0.5,
            detections_per_img=100,
            box_coder=None,
            cls_agnostic_bbox_reg=False,
            bbox_aug_enabled=False
    ):
        """
        Arguments:
            score_thresh (float)
            nms (float)
            detections_per_img (int)
            box_coder (BoxCoder)
        """
        super(PostProcessor, self).__init__()
        self.score_thresh = score_thresh
        self.nms = nms
        self.detections_per_img = detections_per_img
        if box_coder is None:
            box_coder = BoxCoder(weights=(10., 10., 5., 5.))
        self.box_coder = box_coder
        self.cls_agnostic_bbox_reg = cls_agnostic_bbox_reg
        self.bbox_aug_enabled = bbox_aug_enabled

    def forward(self, x, boxes, final_iter=True):
        """
        Arguments:
            x (tuple[tensor, tensor]): x contains the class logits
                and the box_regression from the model.各个预测边框的类别信息列表(预测边框数×类别数）以及他们的边框回归信息(预测边框数×类别数*4）
            boxes (list[BoxList]): bounding boxes that are used as
                reference, one for each image 通过之前网络获得的预测边框列表
        Returns:
            results (list[BoxList]): one BoxList for each image, containing
                the extra fields labels and scores返回最终预测边框信息，附上额外的计算出来的该边框的目标得分以及边框等
        """
        class_logits, box_regression = x
        class_prob = F.softmax(class_logits, -1)  # 分类类别

        # TODO think about a representation of batch of boxes
        image_shapes = [box.size for box in boxes]
        boxes_per_image = [len(box) for box in boxes]
        concat_boxes = torch.cat([a.bbox for a in boxes], dim=0)

        if self.cls_agnostic_bbox_reg:
            box_regression = box_regression[:, -4:]

        proposals = self.box_coder.decode(
            box_regression.view(sum(boxes_per_image), -1), concat_boxes  # 回归值和proposal
        )

        if self.cls_agnostic_bbox_reg:
            proposals = proposals.repeat(1, class_prob.shape[1])

        num_classes = class_prob.shape[1]

        proposals = proposals.split(boxes_per_image, dim=0)
        class_prob = class_prob.split(boxes_per_image, dim=0)

        results = []
        for prob, boxes_per_img, image_shape in zip(
                class_prob, proposals, image_shapes
        ):
            boxlist = self.prepare_boxlist(boxes_per_img, prob, image_shape)
            boxlist = boxlist.clip_to_image(remove_empty=False)
            if not self.bbox_aug_enabled and final_iter:  # If bbox aug is enabled, we will do it later
                boxlist = self.filter_results(boxlist, num_classes)  # 做NMS
            results.append(boxlist)
        return results

    def prepare_boxlist(self, boxes, scores, image_shape):
        """
        Returns BoxList from `boxes` and adds probability scores information
        as an extra field
        `boxes` has shape (#detections, 4 * #classes), where each row represents
        a list of predicted bounding boxes for each of the object classes in the
        dataset (including the background class). The detections in each row
        originate from the same object proposal.
        `scores` has shape (#detection, #classes), where each row represents a list
        of object detection confidence scores for each of the object classes in the
        dataset (including the background class). `scores[i, j]`` corresponds to the
        box at `boxes[i, j * 4:(j + 1) * 4]`.
        从“boxes”返回BoxList并添加概率分数信息，因为额外的字段“boxes”具有shape（#detections，4*#classes），
        其中每一行表示数据集中每个对象类（包括background类）的预测边界框列表。
        每行中的检测源于同一个对象建议。“分数”具有形状（“检测”、“类”），
        其中每行表示数据集中每个对象类（包括背景类）的对象检测置信分数列表。
        `分数[i，j]``对应于`方框[i，j*4:（j+1）*4]处的方框`
        """
        boxes = boxes.reshape(-1, 4)
        scores = scores.reshape(-1)
        boxlist = BoxList(boxes, image_shape, mode="xyxy")
        boxlist.add_field("scores", scores)
        return boxlist

    def filter_results(self, boxlist, num_classes):
        """Returns bounding-box detection results by thresholding on scores and
        applying non-maximum suppression (NMS).
        """
        # unwrap the boxlist to avoid additional overhead.
        # if we had multi-class NMS, we could perform this directly on the boxlist
        boxes = boxlist.bbox.reshape(-1, num_classes * 4)
        scores = boxlist.get_field("scores").reshape(-1, num_classes)

        device = scores.device
        result = []
        # Apply threshold on detection probabilities and apply NMS
        # Skip j = 0, because it's the background class
        inds_all = scores > self.score_thresh
        for j in range(1, num_classes):
            inds = inds_all[:, j].nonzero().squeeze(1)  # 某一张图的某个种类的所有proposal的位置
            scores_j = scores[inds, j]  # 某一proposal的某个种类的得分
            boxes_j = boxes[inds, j * 4: (j + 1) * 4]  # 某一proposal的某个种类的实际坐标
            boxlist_for_class = BoxList(boxes_j, boxlist.size, mode="xyxy")
            boxlist_for_class.add_field("scores", scores_j)
            boxlist_for_class = boxlist_nms(  # 只在同类别中做NMS
                boxlist_for_class, self.nms
            )
            num_labels = len(boxlist_for_class)
            boxlist_for_class.add_field(
                "labels", torch.full((num_labels,), j, dtype=torch.int64, device=device)
            )
            result.append(boxlist_for_class)

        result = cat_boxlist(result)  # 某一张图的NMS后剩下的所有种类的proposal
        number_of_detections = len(result)

        # Limit to max_per_image detections **over all classes**
        if number_of_detections > self.detections_per_img > 0:  # 再按照scores过滤一遍
            cls_scores = result.get_field("scores")
            image_thresh, _ = torch.kthvalue(
                cls_scores.cpu(), number_of_detections - self.detections_per_img + 1
            )
            keep = cls_scores >= image_thresh.item()
            keep = torch.nonzero(keep).squeeze(1)
            result = result[keep]
        return result


def make_roi_box_post_processor(cfg):
    use_fpn = cfg.MODEL.ROI_HEADS.USE_FPN

    bbox_reg_weights = cfg.MODEL.ROI_HEADS.BBOX_REG_WEIGHTS  # (10., 10., 5., 5.)
    box_coder = BoxCoder(weights=bbox_reg_weights)

    score_thresh = cfg.MODEL.ROI_HEADS.SCORE_THRESH  # 0.05
    nms_thresh = cfg.MODEL.ROI_HEADS.NMS  # 0.5
    detections_per_img = cfg.MODEL.ROI_HEADS.DETECTIONS_PER_IMG  # 100
    cls_agnostic_bbox_reg = cfg.MODEL.CLS_AGNOSTIC_BBOX_REG  # False
    bbox_aug_enabled = cfg.TEST.BBOX_AUG.ENABLED

    postprocessor = PostProcessor(
        score_thresh,
        nms_thresh,
        detections_per_img,
        box_coder,
        cls_agnostic_bbox_reg,
        bbox_aug_enabled
    )
    return postprocessor
