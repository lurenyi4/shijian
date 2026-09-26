"""Generate a printed public-domain specimen for smoke testing, not handwriting evaluation."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

root = Path(__file__).parent/'.qa'
root.mkdir(exist_ok=True)
image = Image.new('RGB',(960,1200),'#f6f0e2')
draw = ImageDraw.Draw(image)
font = ImageFont.truetype('C:/Windows/Fonts/simkai.ttf',52)
title = ImageFont.truetype('C:/Windows/Fonts/simkai.ttf',72)
draw.text((325,140),'静夜思',font=title,fill='#302d24')
draw.text((405,280),'李白',font=font,fill='#59503d')
for i,line in enumerate(['床前明月光，','疑是地上霜。','举头望明月，','低头思故乡。']):
    draw.text((320,425+i*135),line,font=font,fill='#302d24')
image.save(root/'printed-sample.png')
