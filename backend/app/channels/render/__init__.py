"""渠道渲染层：把一次回答或一笔待确认操作，变成某个平台能画出来的卡片。

`base` 放渠道无关的部分（状态 → 色调、业务操作 → 中文措辞、要素白名单、金额换算），
`feishu` 放飞书 Interactive Card Schema 2.0 的具体 JSON 结构。接入新渠道时只添一个同级模块，
既不动 `base`，也不动引擎——引擎只认 `base.BaseRenderer` 那三个方法。
"""

from app.channels.render.base import BaseRenderer, CardStatus, CardTone
from app.channels.render.feishu import FeishuCardRenderer, confirmation_action_value

__all__ = [
    "BaseRenderer",
    "CardStatus",
    "CardTone",
    "FeishuCardRenderer",
    "confirmation_action_value",
]
