#!/usr/bin/env python3
"""Version-checked WebView layout compatibility for Safari without CSS anchors."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile


BUNDLE = Path("out/vs/code/browser/workbench/workbench.js")
TEMPLATE = Path("out/vs/code/browser/workbench/workbench.html")
ORIGINAL_SHA256 = "43730cccef6d7fb38daabae0a7a0b99c37bfa4ceb1cfe421e2ecd96e1b8fde27"
# Windows 1.143.0, from the SHA-256-verified windows-amd64.lock.json archive.
WINDOWS_SHA256 = "7580b4a44d3e48a0d5cb8c03b28632f80ff5952dbf07cb80c7051a293ead1975"
START = "var Y4e=class{constructor(){this.content=document.createElement(\"div\")"
END = ";var smi="
MARKER = "/* RemoteCode WebView layout fallback v1 */"
QUERY = "?remotecode-layout=1"

# A factory keeps the native class unchanged on browsers with CSS anchors. The
# second argument is used only by the standalone browser regression fixture.
FACTORY = r"""((NativeOverlay, nativeAnchors =
  CSS.supports('top', 'anchor(top)') &&
  CSS.supports('width', 'anchor-size(width)') &&
  CSS.supports('position-anchor', '--remotecode-probe')) => {
  if (nativeAnchors) return NativeOverlay;
  return class RemoteCodeOverlayLayout {
    constructor() {
      this.content = document.createElement('div');
      this._root = document.createElement('div');
      this._root.appendChild(this.content);
      this._pending = null;
      this._disposed = false;
      this._schedule = () => {
        if (this._disposed || this._pending !== null || !this._win) return;
        this._pending = this._win.requestAnimationFrame(() => {
          this._pending = null;
          this._layout();
        });
      };
      this.reapplyLayoutStyles();
    }
    get root() { return this._root; }
    _set(style, key, value) {
      if (style[key] !== value) style[key] = value;
    }
    reapplyLayoutStyles() {
      Object.assign(this._root.style, {
        position: 'absolute', top: '0px', left: '0px', width: '100%',
        height: '100%', pointerEvents: 'none', overflow: 'visible'
      });
      Object.assign(this.content.style, {
        position: 'absolute', overflow: 'hidden', pointerEvents: 'auto'
      });
      this._schedule();
    }
    _disconnect() {
      this._resize?.disconnect();
      this._mutations?.disconnect();
      if (!this._win) return;
      this._win.removeEventListener('resize', this._schedule);
      this._win.removeEventListener('scroll', this._schedule, true);
      this._win.visualViewport?.removeEventListener('resize', this._schedule);
      this._win.visualViewport?.removeEventListener('scroll', this._schedule);
      if (this._pending !== null) this._win.cancelAnimationFrame(this._pending);
      this._pending = null;
    }
    setAnchorElement(anchor, options) {
      if (this._disposed) return;
      const clip = options?.clippingContainer;
      if (this._anchor !== anchor || this._clip !== clip) {
        this._disconnect();
        this._anchor = anchor;
        this._clip = clip;
        this._win = anchor.ownerDocument.defaultView;
        this.content.dataset.parentFlowToElementId = anchor.id;
        this._resize = new this._win.ResizeObserver(this._schedule);
        this._resize.observe(anchor);
        if (clip && clip !== anchor) this._resize.observe(clip);
        this._mutations = new this._win.MutationObserver(records => {
          if (records.some(record => !this._root.contains(record.target))) this._schedule();
        });
        this._mutations.observe(anchor.ownerDocument.body, {
          subtree: true, childList: true, attributes: true, attributeFilter: ['style', 'class']
        });
        this._win.addEventListener('resize', this._schedule);
        this._win.addEventListener('scroll', this._schedule, true);
        this._win.visualViewport?.addEventListener('resize', this._schedule);
        this._win.visualViewport?.addEventListener('scroll', this._schedule);
      }
      this._layout();
      this._schedule();
    }
    _layout() {
      if (!this._anchor?.isConnected || !this._root.isConnected) {
        this._set(this.content.style, 'width', '0px');
        this._set(this.content.style, 'height', '0px');
        return;
      }
      const root = this._root.getBoundingClientRect();
      const box = this._anchor.getBoundingClientRect();
      const sx = root.width / this._root.offsetWidth || 1;
      const sy = root.height / this._root.offsetHeight || 1;
      for (const [key, value] of Object.entries({
        left: (box.left - root.left) / sx, top: (box.top - root.top) / sy,
        width: box.width / sx, height: box.height / sy
      })) this._set(this.content.style, key, Math.max(key === 'width' || key === 'height' ? 0 : -Infinity, value) + 'px');
      let clipPath = '';
      if (this._clip?.isConnected) {
        const clip = this._clip.getBoundingClientRect();
        const insets = [(clip.top - root.top) / sy, (root.right - clip.right) / sx,
          (root.bottom - clip.bottom) / sy, (clip.left - root.left) / sx];
        clipPath = 'inset(' + insets.map(n => Math.max(0, n) + 'px').join(' ') + ')';
      }
      this._set(this._root.style, 'clipPath', clipPath);
      let zIndex = '';
      for (let node = this._anchor; node; node = node.parentElement) {
        const z = this._win.getComputedStyle(node).zIndex;
        if (z && z !== 'auto') { zIndex = String(Number(z) + 1); break; }
      }
      this._set(this.content.style, 'zIndex', zIndex);
    }
    dispose() {
      this._disposed = true;
      this._disconnect();
      this._root.remove();
    }
  };
})"""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def patched_bundle(original):
    if digest(original) not in (ORIGINAL_SHA256, WINDOWS_SHA256):
        raise ValueError("未知 VS Code workbench 版本；拒绝修改。请先核实版本及 Safari 兼容性。")
    text = original.decode("utf-8")
    start = text.index(START)
    end = text.index(END, start)
    if not text[start:end].endswith("}"):
        raise ValueError("VS Code 布局类边界不匹配，拒绝修改")
    native = text[start + len("var Y4e="):end]
    replacement = MARKER + "var Y4e=" + FACTORY + "(" + native + ")"
    return (text[:start] + replacement + text[end:]).encode("utf-8")


def atomic_write(path, data):
    mode = path.stat().st_mode & 0o777
    fd, name = tempfile.mkstemp(prefix=".remotecode-layout-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def apply_server(server):
    """Back up originals, reject unknown edits, and resume an interrupted apply."""
    bundle, template = server / BUNDLE, server / TEMPLATE
    backup = bundle.with_suffix(".js.remotecode-original")
    data = bundle.read_bytes()
    original = backup.read_bytes() if backup.exists() else data
    expected = patched_bundle(original)
    if data not in (original, expected):
        raise ValueError("workbench 已被其他程序修改；保留文件，拒绝覆盖")
    html = template.read_bytes()
    source = b"/out/vs/code/browser/workbench/workbench.js"
    if html.count(source) != 1:
        raise ValueError("workbench HTML 入口不匹配；拒绝修改")
    if source + QUERY.encode() in html:
        updated = html
    elif source + b'"' in html:
        updated = html.replace(source + b'"', source + QUERY.encode() + b'"', 1)
    else:
        raise ValueError("workbench HTML 缓存版本不匹配；拒绝修改")
    html_backup = template.with_suffix(".html.remotecode-original")
    if html_backup.exists():
        saved = html_backup.read_bytes()
        saved_patched = saved.replace(source + b'"', source + QUERY.encode() + b'"', 1)
        if html not in (saved, saved_patched):
            raise ValueError("workbench HTML 已有其他修改；拒绝覆盖")
    if not backup.exists():
        backup.write_bytes(original)
    if not html_backup.exists():
        html_backup.write_bytes(html)
    if data != expected:
        atomic_write(bundle, expected)
    if html != updated:
        atomic_write(template, updated)
    print("WebView 布局兼容已就绪：支持 CSS 锚点时保留原实现，否则使用尺寸定位；刷新 iPad 页面生效。")
    return {"original_sha256": digest(original), "patched_sha256": digest(expected), "revision": 1}


def restore_server(server):
    bundle, template = server / BUNDLE, server / TEMPLATE
    backup = bundle.with_suffix(".js.remotecode-original")
    html_backup = template.with_suffix(".html.remotecode-original")
    original, html = backup.read_bytes(), html_backup.read_bytes()
    if bundle.read_bytes() not in (original, patched_bundle(original)):
        raise ValueError("workbench 已有其他修改；拒绝覆盖")
    expected_html = html.replace(b'/out/vs/code/browser/workbench/workbench.js"',
                                 b'/out/vs/code/browser/workbench/workbench.js' + QUERY.encode() + b'"', 1)
    if template.read_bytes() not in (html, expected_html):
        raise ValueError("workbench HTML 已有其他修改；拒绝覆盖")
    atomic_write(bundle, original)
    atomic_write(template, html)
    print("已恢复原始 WebView 布局与入口；备份保留。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["apply", "restore"])
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    server = args.root.expanduser().resolve() / "server"
    if args.action == "apply":
        print(json.dumps(apply_server(server), indent=2))
    else:
        restore_server(server)
