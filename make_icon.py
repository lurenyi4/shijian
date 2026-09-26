"""Render the app's typographic brand mark at Windows icon sizes."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

image=Image.new('RGBA',(256,256),(0,0,0,0))
draw=ImageDraw.Draw(image)
draw.rounded_rectangle((6,6,250,250),radius=46,fill='#3b5140')
draw.rounded_rectangle((26,26,230,230),radius=28,outline='#a6b295',width=3)
font=ImageFont.truetype('C:/Windows/Fonts/simkai.ttf',178)
draw.text((128,118),'拾',font=font,anchor='mm',fill='#f1efdc',stroke_width=0)
image.save(Path(__file__).parent/'static'/'app.ico',sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
