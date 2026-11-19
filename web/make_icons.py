#!/usr/bin/env python3
"""Draw the app icons (icon-2-*.png) into the given folder. Needs Pillow; run on a dev box, not the Pi."""
import sys
from PIL import Image, ImageDraw, ImageFilter
NIGHT=(35,38,74); NIGHT2=(58,64,120); MOON=(244,201,93); WHITE=(255,255,255)
def icon(size, maskable, out):
    S=size*4
    im=Image.new("RGBA",(S,S),(0,0,0,0))
    bg=Image.new("RGBA",(S,S))
    px=bg.load()
    for y in range(S):
        t=y/S
        c=tuple(int(NIGHT[i]*(1-t)+NIGHT2[i]*t) for i in range(3))
        for x in range(S): px[x,y]=c+(255,)
    mask=Image.new("L",(S,S),0); d=ImageDraw.Draw(mask)
    if maskable: d.rectangle([0,0,S,S],fill=255)
    else: d.rounded_rectangle([0,0,S-1,S-1],radius=int(S*0.22),fill=255)
    im.paste(bg,(0,0),mask)
    d=ImageDraw.Draw(im)
    k=0.78 if maskable else 1.0      # keep art inside the maskable safe zone
    cx=cy=S/2
    def P(x,y): return (cx+(x-0.5)*S*k, cy+(y-0.5)*S*k)
    def R(r): return r*S*k
    # stars
    for (x,y,r) in ((0.20,0.24,0.012),(0.30,0.80,0.009),(0.80,0.18,0.010),(0.86,0.70,0.008),(0.14,0.58,0.007)):
        X,Y=P(x,y); d.ellipse([X-R(r),Y-R(r),X+R(r),Y+R(r)],fill=WHITE+(200,))
    # crescent moon: full disc minus offset disc
    moon=Image.new("L",(S,S),0); md=ImageDraw.Draw(moon)
    X,Y=P(0.44,0.50); r=R(0.30); md.ellipse([X-r,Y-r,X+r,Y+r],fill=255)
    X2,Y2=P(0.56,0.40); r2=R(0.27); md.ellipse([X2-r2,Y2-r2,X2+r2,Y2+r2],fill=0)
    im.paste(Image.new("RGBA",(S,S),MOON+(255,)),(0,0),moon)
    # music note (eighth note) in white, nestled in the crescent's hollow
    hx,hy=P(0.60,0.56); hr=R(0.062)
    note=Image.new("L",(int(hr*3),int(hr*3)),0); ImageDraw.Draw(note).ellipse([hr*0.25,hr*0.5,hr*2.75,hr*2.5],fill=255); note=note.rotate(22,resample=Image.BICUBIC); im.paste(Image.new("RGBA",note.size,WHITE+(255,)),(int(hx-hr*1.5),int(hy-hr*1.5)),note)
    sx=hx+hr*1.05; top=P(0,0.22)[1]
    d.rectangle([sx-R(0.022),top,sx,hy],fill=WHITE)
    d.polygon([(sx-R(0.01),top),(sx+R(0.13),top+R(0.10)),(sx+R(0.13),top+R(0.17)),(sx-R(0.01),top+R(0.08))],fill=WHITE)
    im=im.resize((size,size),Image.LANCZOS)
    im.save(out)
o=sys.argv[1]
icon(192,False,f"{o}/icon-2-192.png"); icon(512,False,f"{o}/icon-2-512.png"); icon(512,True,f"{o}/icon-2-maskable-512.png")
