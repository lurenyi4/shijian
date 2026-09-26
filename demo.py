"""Public-domain poetry and attributed museum illustrations, never passed off as OCR."""
import json
from pathlib import Path

ART = Path(__file__).resolve().parent/"static"/"art"

POEMS = [
    ("山居秋暝", "王维", "唐", "空山新雨后，天气晚来秋。\n明月松间照，清泉石上流。\n竹喧归浣女，莲动下渔舟。\n随意春芳歇，王孙自可留。", "山水清音", "landscape"),
    ("饮酒·其五", "陶渊明", "东晋", "结庐在人境，而无车马喧。\n问君何能尔？心远地自偏。\n采菊东篱下，悠然见南山。\n山气日夕佳，飞鸟相与还。\n此中有真意，欲辨已忘言。", "人间有味", "mountains"),
    ("春夜喜雨", "杜甫", "唐", "好雨知时节，当春乃发生。\n随风潜入夜，润物细无声。\n野径云俱黑，江船火独明。\n晓看红湿处，花重锦官城。", "四时诗意", "masters"),
    ("定风波·莫听穿林打叶声", "苏轼", "宋", "莫听穿林打叶声，何妨吟啸且徐行。\n竹杖芒鞋轻胜马，谁怕？一蓑烟雨任平生。\n\n料峭春风吹酒醒，微冷，山头斜照却相迎。\n回首向来萧瑟处，归去，也无风雨也无晴。", "人间有味", "bamboo"),
    ("枫桥夜泊", "张继", "唐", "月落乌啼霜满天，江枫渔火对愁眠。\n姑苏城外寒山寺，夜半钟声到客船。", "山水清音", "autumn"),
    ("小池", "杨万里", "宋", "泉眼无声惜细流，树阴照水爱晴柔。\n小荷才露尖尖角，早有蜻蜓立上头。", "四时诗意", "lotus"),
]



def artwork(key):
    sources = json.loads((ART/'sources.json').read_text(encoding='utf-8-sig'))
    source = next(s for s in sources if s['file'] == key+'.jpg')
    return ART/source['file'], source


def artwork_note(source):
    return f"配图：{source['title']}；The Metropolitan Museum of Art，Public Domain / CC0。{source['source']}\n配图与诗文为赏读搭配，不是诗作原稿，也不是对此图的识别结果。"
