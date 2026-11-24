"""
anime_graph_bg.py  -  shows the anime theme art in IDA's graph view.

IDA paints the graph canvas itself with a solid colour, so a stylesheet image
can't show through there. This plugin puts a click-through layer over each
graph canvas and blends the theme's art.png into its bottom-right corner:

  * dark themes use "lighten" blending: art shows wherever it is brighter
    than the background, while brighter code text stays on top
  * light themes use "darken" blending for the same effect

It reads the active theme from IDA's stylesheet, follows theme switches
automatically, and does nothing for themes without an art.png.

Install: copy this file to  %APPDATA%\\Hex-Rays\\IDA Pro\\plugins\\  and restart IDA.
Requires IDA 9.x with PySide6.
"""
import os
import re

import ida_diskio
import ida_idaapi
import ida_kernwin
import shiboken6
from PySide6 import QtCore, QtGui, QtWidgets

THEME_OVERRIDE = None   # e.g. "natsu-matsuri", only if auto-detection fails
ART_OPACITY = 0.9       # 0.0 - 1.0
DEBUG = False


def _log(*a):
    if DEBUG:
        print("[anime_graph_bg]", *a)


def _all_stylesheets():
    app = QtWidgets.QApplication.instance()
    parts = [app.styleSheet()] + [w.styleSheet() for w in app.topLevelWidgets()]
    return "\n".join(p for p in parts if p)


def _user_themes_dir():
    return os.path.join(ida_diskio.get_user_idadir(), "themes")


_KEY_RE = re.compile(r"qproperty-graph-bg-top:\s*rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)")
_INSN_RE = re.compile(r"qproperty-line-fg-insn:\s*(#[0-9a-fA-F]{6})")
_URL_RE = re.compile(r'url\(\s*"?([^")]*art\.png)"?\s*\)')


def _detect_theme_dir(css):
    """Folder of the active anime theme, or None."""
    root = _user_themes_dir()
    if THEME_OVERRIDE:
        d = os.path.join(root, THEME_OVERRIDE)
        return d if os.path.isfile(os.path.join(d, "art.png")) else None
    for m in _URL_RE.finditer(css):          # $RELPATH already expanded by IDA?
        p = m.group(1)
        if "$RELPATH" not in p and os.path.isfile(p):
            return os.path.dirname(p)
    key, insn = _KEY_RE.search(css), _INSN_RE.search(css)
    if not (key and insn) or not os.path.isdir(root):
        return None
    sig_key = "rgba(%s, %s, %s, 0)" % key.groups()
    sig_insn = "qproperty-line-fg-insn: %s" % insn.group(1).lower()
    for name in sorted(os.listdir(root)):
        css_path = os.path.join(root, name, "theme.css")
        if not (os.path.isfile(css_path) and os.path.isfile(os.path.join(root, name, "art.png"))):
            continue
        with open(css_path, encoding="utf-8", errors="ignore") as f:
            text = f.read().lower()
        if sig_key in text and sig_insn in text:
            return os.path.join(root, name)
    return None


class _ArtLayer(QtWidgets.QWidget):
    """Transparent, click-through layer drawn over a graph canvas."""

    def __init__(self, canvas, core):
        super().__init__(canvas)
        self.core = core
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.setGeometry(canvas.rect())
        canvas.installEventFilter(self)
        self.raise_()
        self.show()

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t == QtCore.QEvent.Type.Resize:
            self.setGeometry(obj.rect())
            self.raise_()
        elif t == QtCore.QEvent.Type.ChildAdded:
            QtCore.QTimer.singleShot(0, self.raise_)
        elif t == QtCore.QEvent.Type.StyleChange:
            self.core.schedule_refresh()          # theme switched
        return False

    def _text_mode(self):
        """True unless this IDA View is currently rendering the graph.

        IDAView is a scroll area whose viewport ('qt_scrollarea_viewport') is
        reused for BOTH the text listing and the graph, so the widget tree can't
        tell the modes apart. Ask IDA for the view's renderer instead. The text
        listing already draws the art via the theme CSS, so in text mode the
        layer must stay empty or the picture appears twice. Fails safe: any
        error counts as text mode (draw nothing)."""
        try:
            host = self.parentWidget().parentWidget()
            tw = ida_kernwin.TWidget__from_ptrval__(shiboken6.Shiboken.getCppPointer(host)[0])
            return ida_kernwin.get_view_renderer_type(tw) != ida_kernwin.TCCRT_GRAPH
        except Exception:
            return True

    def paintEvent(self, ev):
        try:
            self._paint(ev)
        except Exception:
            pass                                  # fail safe: draw nothing

    def _paint(self, ev):
        st = self.core.state
        if not st or self._text_mode():
            return
        dpr = self.devicePixelRatioF()
        art = self.core.art(dpr)          # pre-scaled to device pixels, opacity baked in
        if art is None:
            return
        # bottom-right, on whole device pixels -> a plain blit, no per-paint scaling
        x = (round(self.width() * dpr) - art.width()) / dpr
        y = (round(self.height() * dpr) - art.height()) / dpr
        p = QtGui.QPainter(self)
        p.setClipRegion(ev.region())
        p.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_Darken if st["light"]
                             else QtGui.QPainter.CompositionMode.CompositionMode_Lighten)
        p.drawPixmap(QtCore.QPointF(x, y), art)
        p.end()


def _hook_view(core, w):
    """Put a layer on IDAView w's scroll-area viewport (the graph canvas)."""
    if w is None or w.metaObject().className() != "IDAView":
        return
    for c in w.children():
        if (isinstance(c, QtWidgets.QWidget) and c.objectName() == "qt_scrollarea_viewport"
                and c.metaObject().className() == "QWidget" and not c.property("_anime_graph_bg")):
            c.setProperty("_anime_graph_bg", True)
            core.layers.append(_ArtLayer(c, core))
            _log("hooked graph canvas")


class _Core:
    """No polling and no IDA hooks: views are found by a few one-off scans after
    start-up and whenever keyboard focus moves (a new IDA View gets focus before
    you can press Space in it); a theme switch arrives as a Qt StyleChange."""

    STARTUP_SCANS_MS = (0, 1000, 3000, 10000)

    def __init__(self):
        self.state = None
        self._sig = None
        self._pix = {}
        self.layers = []
        self._pending = False
        app = QtWidgets.QApplication.instance()
        app.focusChanged.connect(self._on_focus)
        for ms in self.STARTUP_SCANS_MS:
            QtCore.QTimer.singleShot(ms, self.full_scan)

    def _on_focus(self, old, new):
        try:
            w = new
            while w is not None:
                if w.metaObject().className() == "IDAView":
                    self.on_widget(w)
                    return
                w = w.parentWidget()
        except Exception:
            pass

    def stop(self):
        try:
            QtWidgets.QApplication.instance().focusChanged.disconnect(self._on_focus)
        except Exception:
            pass
        for layer in self.layers:
            try:
                layer.parentWidget().removeEventFilter(layer)
                layer.deleteLater()
            except RuntimeError:
                pass
        self.layers = []

    def art(self, dpr):
        """Art for this display's pixel ratio, built once and cached.

        Scaled (smoothly) to exact device pixels with ART_OPACITY baked in, so a
        paint is a plain blit. On scaled displays (e.g. 125%) it downscales the
        2x art rather than upscaling the 1x art. Only the result is kept."""
        st = self.state
        key = round(dpr, 3)
        pm = self._pix.get(key)
        if pm is None:
            base = QtGui.QImageReader(os.path.join(st["dir"], "art.png")).size()
            src = QtGui.QImage(os.path.join(st["dir"], "art@2x.png" if (dpr > 1.0 and st["has2x"])
                                            else "art.png"))
            if src.isNull() or not base.isValid():
                return None
            src.setDevicePixelRatio(1.0)   # Qt tags "@2x" files as DPR 2; work in raw pixels
            dw, dh = round(base.width() * dpr), round(base.height() * dpr)
            src = src.convertToFormat(QtGui.QImage.Format.Format_ARGB32_Premultiplied)
            if (src.width(), src.height()) != (dw, dh):
                src = src.scaled(dw, dh, QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
                                 QtCore.Qt.TransformationMode.SmoothTransformation)
            img = QtGui.QImage(dw, dh, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
            img.fill(QtCore.Qt.GlobalColor.transparent)
            p = QtGui.QPainter(img)
            p.setOpacity(ART_OPACITY)
            p.drawImage(0, 0, src)
            p.end()
            pm = QtGui.QPixmap.fromImage(img)
            pm.setDevicePixelRatio(dpr)
            self._pix[key] = pm
        return pm

    def _refresh_state(self):
        css = _all_stylesheets()
        sig = (len(css), hash(css), THEME_OVERRIDE)
        if sig == self._sig:
            return False
        self._sig = sig
        self._pix = {}
        tdir = _detect_theme_dir(css)
        m = _KEY_RE.search(css)
        if not (tdir and m):
            self.state = None
            _log("no anime theme active")
            return True
        key = QtGui.QColor(*map(int, m.groups()))
        self.state = {"dir": tdir, "light": key.lightnessF() > 0.5,
                      "has2x": os.path.isfile(os.path.join(tdir, "art@2x.png"))}
        _log("theme:", tdir, "light" if self.state["light"] else "dark")
        return True

    def _prune(self):
        alive = []
        for layer in self.layers:
            try:
                layer.objectName()
                alive.append(layer)
            except RuntimeError:
                pass
        self.layers = alive

    def _repaint_all(self):
        self._prune()
        for layer in self.layers:
            layer.update()

    def schedule_refresh(self):
        """Coalesce the burst of StyleChange events a theme switch sends."""
        if not self._pending:
            self._pending = True
            QtCore.QTimer.singleShot(0, self._do_refresh)

    def _do_refresh(self):
        self._pending = False
        try:
            if self._refresh_state():
                self._repaint_all()
        except Exception:
            self.state = None

    def on_widget(self, w):
        try:
            if self.state is None:
                self._refresh_state()
            self._prune()
            _hook_view(self, w)
        except Exception:
            pass

    def full_scan(self):
        """One-off scan (plugin load / UI ready) for views that already exist."""
        try:
            self._refresh_state()
            self._prune()
            for w in QtWidgets.QApplication.allWidgets():
                try:
                    _hook_view(self, w)
                except RuntimeError:
                    pass
            self._repaint_all()
        except Exception:
            self.state = None


class AnimeGraphBG(ida_idaapi.plugin_t):
    flags = ida_idaapi.PLUGIN_FIX | ida_idaapi.PLUGIN_HIDE
    comment = "Show anime theme art in graph view"
    help = ""
    wanted_name = "Anime graph background"
    wanted_hotkey = ""

    def init(self):
        self.core = _Core()
        return ida_idaapi.PLUGIN_KEEP

    def run(self, arg):
        pass

    def term(self):
        self.core.stop()


def PLUGIN_ENTRY():
    return AnimeGraphBG()
