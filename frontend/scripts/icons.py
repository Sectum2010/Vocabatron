"""Reproducible opaque app icons drawn from the public vector mark."""
from pathlib import Path
from PIL import Image,ImageDraw

root=Path(__file__).resolve().parents[1]/'public/icons'
for name,size,maskable in [('icon-192.png',192,False),('icon-512.png',512,False),('maskable-512.png',512,True),('apple-touch-icon.png',180,False)]:
    image=Image.new('RGB',(size,size),'#315840');draw=ImageDraw.Draw(image)
    scale=size/64*(.8 if maskable else 1);origin=(size-64*scale)/2
    for x,y in [(14,14),(27,14),(40,14),(27,27),(14,40),(27,40),(40,40)]:
        draw.rounded_rectangle((origin+x*scale,origin+y*scale,origin+(x+10)*scale,origin+(y+10)*scale),radius=scale,fill='#f7f8f4')
    image.save(root/name,format='PNG',optimize=False)
