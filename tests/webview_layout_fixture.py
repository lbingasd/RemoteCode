#!/usr/bin/env python3
"""Generate a loopback browser fixture exercising the unsupported-anchor branch."""
import argparse
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from webview_compat import FACTORY


def build(folder, acp_html=None):
    folder.mkdir(parents=True, exist_ok=True)
    chat = '<!doctype html><html><body><h2>Chat layout fixture</h2><textarea placeholder="Type a message..."></textarea></body></html>'
    if acp_html:
        chat = json.loads(acp_html.read_text())["html"]
        nonce = re.search(r'<script nonce="([^"]+)"', chat).group(1)
        # A local inert API stub: no agent, login or model requests.
        stub = '<script nonce="' + nonce + '">window.acquireVsCodeApi=()=>({getState:()=>null,setState:()=>{},postMessage:()=>{}});</script>'
        chat = chat.replace('<head>', '<head>' + stub, 1)
    (folder / 'chat.html').write_text(chat)
    (folder / 'factory.js').write_text('const makeOverlay = ' + FACTORY + ';')
    (folder / 'index.html').write_text('''<!doctype html><html><head><meta charset="utf-8">
<title>RemoteCode WebView layout regression</title>
<style>
body {margin:0;background:#fafafd;color:#222;font:14px system-ui}
header {height:60px;padding:8px;box-sizing:border-box}
button {margin:2px;padding:5px 9px}
#app {position:relative;width:100%;height:620px;overflow:hidden;background:#eee;transform-origin:top left}
#sidebar {position:absolute;left:48px;top:30px;width:360px;height:540px;overflow:auto;background:#fff;border:1px solid #999}
#spacer {height:30px} #anchor {height:480px;width:100%} #tail {height:500px}
#log {position:absolute;left:640px;top:40px;white-space:pre-wrap;background:#fff;padding:12px}
</style></head><body><header>
<button id="baseline">Baseline</button><button id="resize">Resize and move</button>
<button id="scroll">Scroll and clip</button><button id="hide">Hide</button>
<button id="show">Show</button><button id="zoom">Scale 0.8</button><button id="dispose">Dispose</button>
</header><div id="app"><div id="sidebar"><div id="spacer"></div><div id="anchor"></div><div id="tail"></div></div><pre id="log"></pre></div>
<script src="factory.js"></script><script>
const app=document.querySelector('#app'), sidebar=document.querySelector('#sidebar'),
 anchor=document.querySelector('#anchor'), log=document.querySelector('#log');
const Native=class {}; const Layout=makeOverlay(Native,false);
const overlay=new Layout(); app.appendChild(overlay.root);
const iframe=document.createElement('iframe'); iframe.src='chat.html';
Object.assign(iframe.style,{border:'none',width:'100%',height:'100%'});
overlay.content.appendChild(iframe);
overlay.setAnchorElement(anchor,{clippingContainer:sidebar});
function write(s){log.textContent+=s+'\\n'}
write('Native branch preserved: '+(makeOverlay(Native,true)===Native?'PASS':'FAIL'));
write('Unsupported-anchor branch forced for this fixture');
function verify(name){requestAnimationFrame(()=>requestAnimationFrame(()=>{
 const a=anchor.getBoundingClientRect(), b=iframe.getBoundingClientRect();
 const keys=name==='hide'?['width','height']:['left','top','width','height'];
 const delta=Math.max(...keys.map(k=>Math.abs(a[k]-b[k])));
 write(name+': '+(delta<1?'PASS':'FAIL')+' delta='+delta.toFixed(2)+' iframe='+Math.round(b.width)+'x'+Math.round(b.height));
 if(name==='scroll')write('Clipping: '+(overlay.root.style.clipPath.startsWith('inset(')?'PASS':'FAIL'));
}));}
document.querySelector('#baseline').onclick=()=>verify('baseline');
document.querySelector('#resize').onclick=()=>{Object.assign(sidebar.style,{left:'240px',top:'60px',width:'320px',height:'360px'});anchor.style.height='300px';verify('resize/move')};
document.querySelector('#scroll').onclick=()=>{sidebar.scrollTop=80;verify('scroll')};
document.querySelector('#hide').onclick=()=>{anchor.style.display='none';verify('hide')};
document.querySelector('#show').onclick=()=>{anchor.style.display='block';verify('show')};
document.querySelector('#zoom').onclick=()=>{app.style.transform='scale(.8)';verify('scale')};
document.querySelector('#dispose').onclick=()=>{overlay.dispose();write('Dispose: '+(!overlay.root.isConnected?'PASS':'FAIL'));sidebar.style.width='400px'};
iframe.addEventListener('load',()=>verify('initial'));
</script></body></html>''')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--acp-html', type=Path)
    args = parser.parse_args()
    build(args.output, args.acp_html)
