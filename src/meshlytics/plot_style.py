"""
plot_style.py

Shared figure theme and helpers used by every Meshlytics figure:
- One font family and a three-size ladder mapped to role
  (titles + axis labels | legends + annotations | tick labels), scalable with --font-scale
- Open frame (left/bottom axis lines), outward ticks, boxless legends, light value grid only where it helps
- Restrained palette: one primary blue for single-series figures, muted greys for context,
  the colour-blind-safe Okabe-Ito set for categories, a single-hue sequential map for heatmaps
- One colour per MeSH term for the whole run (the same term looks the same in every figure)
- save_figure(): one place for resolution, file formats and the saved-path message
- Label helpers: wrap/shorten long labels, k/M number formatting, figure height from bar count,
  direct labels at line ends, text-overlap check

Global settings are changed with configure_figures(), called from cli.py (analyze flags
--fig-dpi, --fig-format, --font-scale).
"""

import os
import textwrap

import matplotlib
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap, to_rgb
from matplotlib import font_manager
from matplotlib.ticker import MaxNLocator, FuncFormatter


# -------------------------------------------------------------------------
# Palette
# -------------------------------------------------------------------------

PRIMARY = "#2F5D8C"        # single-series bars/lines
PRIMARY_LIGHT = "#A9C0D8"  # secondary marks of the primary series
TEXT = "#222222"
GREY_DARK = "#555555"      # secondary text, edges
GREY = "#8C8C8C"           # context marks
GREY_LIGHT = "#DDDDDD"     # grid, light edges
OTHER = "#BDBDBD"          # 'Other' categories / small communities

# Okabe-Ito colour-blind-safe set (yellow last: weakest on white)
CATEGORICAL = [
    "#0072B2",  # blue
    "#E69F00",  # orange
    "#009E73",  # bluish green
    "#CC79A7",  # reddish purple
    "#56B4E9",  # sky blue
    "#D55E00",  # vermillion
    "#7F7F7F",  # grey
    "#000000",  # black
    "#F0E442",  # yellow
]

def _tint(hex_color, amount=0.45):
    r, g, b = to_rgb(hex_color)
    return (r + (1 - r) * amount, g + (1 - g) * amount, b + (1 - b) * amount)


# 9 strong colours, then 8 lighter tints of the same hues (used only when a figure needs more categories)
# Term colours: yellow (weak on white) and black (too heavy) are left out
# grey is reserved for 'other' terms; tints of the two blues are skipped (too close to sky blue)
_TERM_BASE = ["#0072B2", "#E69F00", "#009E73", "#CC79A7", "#56B4E9", "#D55E00"]
# terms 7-10: Paul Tol 'muted' colours that differ from the six Okabe-Ito hues (indigo, wine, olive, sand)
EXTENDED = _TERM_BASE + ["#332288", "#882255", "#999933", "#DDCC77"]
TERM_GREYS = ["#9E9E9E", "#BDBDBD", "#7A7A7A", "#D0D0D0", "#B0B0B0", "#8A8A8A"]   # terms beyond EXTENDED

# Single-hue sequential map for counts (white -> deep primary blue)
SEQUENTIAL = LinearSegmentedColormap.from_list("meshlytics_blues", ["#F5F8FC", "#C6D7EA", "#6F98C2", PRIMARY, "#16324F"])


# -------------------------------------------------------------------------
# Global figure settings
# -------------------------------------------------------------------------

FIGURE_SETTINGS = {
    "dpi": 300,           # --fig-dpi
    "formats": ["png"],   # --fig-format png pdf svg
    "font_scale": 1.0,    # --font-scale
}

# Base ladder in points at font_scale = 1.0 (role -> size)
_BASE_SIZES = {"large": 13, "medium": 11, "small": 10}


def font_size(role="large"):
    """Font size in points for a role: 'large' (titles, axis labels), 'medium' (legend, annotations), 'small' (ticks)."""
    return round(_BASE_SIZES[role] * FIGURE_SETTINGS["font_scale"], 1)


def _font_family():
    available = {f.name for f in font_manager.fontManager.ttflist}
    for name in ("Arial", "Helvetica", "Liberation Sans", "DejaVu Sans"):
        if name in available:
            return name
    return "sans-serif"


def apply_theme():
    """Sets matplotlib rcParams for all Meshlytics figures (called on import and by configure_figures)."""
    family = _font_family()
    matplotlib.rcParams.update({
        "font.family": "sans-serif",
        "font.sans-serif": [family, "DejaVu Sans"],
        "font.size": font_size("medium"),
        "axes.titlesize": font_size("large"),
        "axes.titleweight": "bold",
        "axes.titlelocation": "left",
        "axes.titlepad": 10,
        "axes.labelsize": font_size("large"),
        "axes.labelcolor": TEXT,
        "axes.edgecolor": GREY_DARK,
        "axes.linewidth": 0.8,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": False,
        "axes.axisbelow": True,
        "axes.prop_cycle": matplotlib.cycler(color=CATEGORICAL),
        "xtick.labelsize": font_size("small"),
        "ytick.labelsize": font_size("small"),
        "xtick.color": GREY_DARK,
        "ytick.color": GREY_DARK,
        "xtick.labelcolor": TEXT,
        "ytick.labelcolor": TEXT,
        "xtick.direction": "out",
        "ytick.direction": "out",
        "xtick.major.size": 3.5,
        "ytick.major.size": 3.5,
        "legend.fontsize": font_size("medium"),
        "legend.title_fontsize": font_size("medium"),
        "legend.frameon": False,
        "figure.facecolor": "white",
        "savefig.facecolor": "white",
        "figure.dpi": 100,
        "savefig.dpi": FIGURE_SETTINGS["dpi"],
        "pdf.fonttype": 42,   # editable text in PDF/SVG
        "ps.fonttype": 42,
        "svg.fonttype": "none",
        "text.color": TEXT,
    })


def configure_figures(dpi=None, formats=None, font_scale=None):
    """Changes the global figure settings (from CLI flags) and re-applies the theme."""
    if dpi is not None:
        FIGURE_SETTINGS["dpi"] = int(dpi)
    if formats:
        allowed = {"png", "pdf", "svg"}
        bad = [f for f in formats if f.lower() not in allowed]
        if bad:
            raise ValueError(f"Unsupported figure format(s): {bad}. Use png, pdf and/or svg.")
        FIGURE_SETTINGS["formats"] = [f.lower() for f in formats]
    if font_scale is not None:
        if font_scale <= 0:
            raise ValueError("font_scale must be > 0")
        FIGURE_SETTINGS["font_scale"] = float(font_scale)
    apply_theme()


# -------------------------------------------------------------------------
# Consistent colour per MeSH term (for the whole run)
# -------------------------------------------------------------------------

_TERM_COLORS = {}


def term_color(term):
    """
    Returns the run-wide colour of a MeSH term. The first 10 terms get distinct colours
    (6 Okabe-Ito hues, then 4 Paul Tol 'muted' hues); later terms get greys and are labelled directly where possible.
    """
    if term not in _TERM_COLORS:
        i = len(_TERM_COLORS)
        _TERM_COLORS[term] = EXTENDED[i] if i < len(EXTENDED) else TERM_GREYS[(i - len(EXTENDED)) % len(TERM_GREYS)]
    return _TERM_COLORS[term]


def is_grey_term(term):
    """True if the term only received a grey (no distinct colour left)."""
    return term_color(term) in TERM_GREYS


def reset_term_colors(ordered_terms=None):
    """Starts a new colour assignment; ordered_terms (most important first) get the first colours."""
    _TERM_COLORS.clear()
    for t in ordered_terms or []:
        term_color(t)


def community_colors(n_communities, max_distinct=8):
    """
    Colours for network communities (largest first). Up to max_distinct communities get distinct
    colours from the term palette (Okabe-Ito blue/orange/green/pink/sky/vermillion, then Tol indigo/wine;
    no yellow, grey or black); the rest share the 'Other' grey.
    """
    distinct = EXTENDED[:max_distinct]
    return [distinct[i] if i < len(distinct) else OTHER for i in range(n_communities)]


# -------------------------------------------------------------------------
# Text helpers
# -------------------------------------------------------------------------

def wrap_label(text, width=32, max_lines=2):
    """Wraps a long label onto at most max_lines lines; the last line is shortened with '...'."""
    text = str(text)
    lines = textwrap.wrap(text, width=width) or [text]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        if len(lines[-1]) > width - 3:
            lines[-1] = lines[-1][:width - 3].rstrip()
        lines[-1] = lines[-1].rstrip(" ,;:") + "..."
    return "\n".join(lines)


def shorten(text, max_chars=40):
    """Single-line shortening with '...'."""
    text = str(text)
    return text if len(text) <= max_chars else text[:max_chars - 3].rstrip(" ,;:") + "..."


def fmt_count(value, _pos=None, digits=1):
    """Counts below 10,000 in full with a thousands separator (1,050); larger ones abbreviated (12.5k, 2.5M)."""
    value = float(value)
    if abs(value) < 10_000:
        return f"{value:,.0f}" if value == int(value) else f"{value:,.2f}".rstrip("0").rstrip(".")
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(value) >= div:
            s = f"{value / div:.{digits}f}".rstrip("0").rstrip(".")
            return f"{s}{suffix}"


COUNT_FORMATTER = FuncFormatter(fmt_count)


def text_color_for(background):
    """Black or white text, whichever contrasts more with the background colour."""
    r, g, b = to_rgb(background)
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return TEXT if luminance > 0.55 else "white"


# -------------------------------------------------------------------------
# Layout helpers
# -------------------------------------------------------------------------

def bar_figure_height(n_bars, per_bar=0.34, base=1.6, min_height=3.0, max_height=16.0):
    """Figure height (in) for a horizontal bar chart with n_bars bars (smaller minimum for 1-3 bars)."""
    if n_bars <= 3:
        min_height = 2.0 + 0.35 * n_bars
    return max(min_height, min(max_height, base + per_bar * n_bars))


def style_axes(ax, grid=None, integer_x=False, integer_y=False):
    """Applies the open frame; grid='x' or 'y' adds a light value grid behind the data."""
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    if grid in ("x", "y", "both"):
        ax.grid(axis=grid, color=GREY_LIGHT, linewidth=0.6)
        ax.set_axisbelow(True)
    if integer_x:
        ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    if integer_y:
        ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    return ax


def wrap_at_separators(text, max_chars, sep=" | "):
    """Wraps a 'part | part | part' subtitle so that lines break only between parts (long parts still wrap)."""
    lines, cur = [], ""
    for part in text.split(sep):
        cand = part if not cur else cur + sep + part
        if len(cand) <= max_chars or not cur:
            cur = cand
        else:
            lines.append(cur)
            cur = part
    lines.append(cur)
    out = []
    for line in lines:
        out.extend(textwrap.wrap(line, width=max_chars) if len(line) > max_chars else [line])
    return "\n".join(out)


def nice_sqrt_ticks(vmax, n=5):
    """Colourbar ticks for a square-root scale: roughly evenly spaced on screen, rounded to round numbers (1, 1.5, 2, 2.5, 3, 4, 5, 6, 7.5, 8 x 10^k)."""
    import math
    ticks = [0]
    for f in [i / (n - 1) for i in range(1, n)]:
        x = vmax * f ** 2
        e = 10 ** math.floor(math.log10(x))
        cands = [m * e for m in (1, 1.5, 2, 2.5, 3, 4, 5, 6, 7.5, 8, 10) if m * e <= vmax]
        t = min(cands, key=lambda c: abs(c - x)) if cands else x
        if t > ticks[-1]:
            ticks.append(t)
    return ticks


def set_title(ax, title, subtitle=None):
    """
    Left-aligned title in bold, with an optional subtitle line (n, period, settings) in grey.
    The subtitle carries the context, so the title can stay short and plain.
    """
    if subtitle:
        # wrap the subtitle to the axes width
        fig = ax.figure
        ax_width_in = ax.get_position().width * fig.get_figwidth()
        max_chars = max(40, int(ax_width_in * 72 / (font_size("medium") * 0.52)))
        subtitle = wrap_at_separators(str(subtitle), max_chars)
        pad = 6
        ax.set_title(subtitle, loc="left", fontsize=font_size("medium"), color=GREY_DARK,
                     fontweight="normal", pad=pad)
        n_lines = str(subtitle).count("\n") + 1
        offset = pad + font_size("medium") * 1.25 * n_lines + 3   # points above the axes top
        ax.annotate(title, xy=(0, 1), xycoords="axes fraction", xytext=(0, offset), textcoords="offset points",
                    ha="left", va="bottom", fontsize=font_size("large"), fontweight="bold", color=TEXT,
                    annotation_clip=False)
    else:
        ax.set_title(title, loc="left")


def end_of_line_labels(ax, series, x_last=None, min_gap_frac=0.055, fontsize=None, x_pad_frac=0.012):
    """
    Direct labels at the right end of line series instead of a legend.

    series : list of (label, y_last, color) or (label, y_last, color, x_last_of_series)
    x_last : x position of the last data point (used when a series has no own x)
    Labels are spread vertically so they never overlap (min_gap_frac of the y range apart).
    """
    if not series:
        return []
    series = [tuple(s) + ((x_last,) if len(s) == 3 else ()) for s in series]
    ymin, ymax = ax.get_ylim()
    gap = (ymax - ymin) * min_gap_frac * FIGURE_SETTINGS["font_scale"]
    order = sorted(series, key=lambda s: s[1])
    positions = []
    for _, y, _, _ in order:
        y_pos = y if not positions else max(y, positions[-1] + gap)
        positions.append(y_pos)
    # shift down if the top label runs past the axis
    overflow = positions[-1] - (ymax - gap * 0.3)
    if overflow > 0:
        positions = [p - overflow for p in positions]
        for i in range(1, len(positions)):
            positions[i] = max(positions[i], positions[i - 1] + gap)
    xmin, xmax = ax.get_xlim()
    texts = []
    for (label, _y, color, x_end), y_pos in zip(order, positions, strict=False):
        x_text = x_end + (xmax - xmin) * x_pad_frac
        texts.append(ax.text(x_text, y_pos, label, color=color, va="center", ha="left",
                             fontsize=fontsize or font_size("medium"), clip_on=False))
    return texts


# -------------------------------------------------------------------------
# Saving and checking
# -------------------------------------------------------------------------

def find_text_overlaps(fig):
    """
    Returns pairs of overlapping visible text strings in a figure (tick labels on their own axis
    are ignored when they touch the spine). Used by save_figure when MESHLYTICS_FIG_CHECK=1.
    """
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    texts = [t for t in fig.findobj(matplotlib.text.Text) if t.get_visible() and t.get_text().strip()]
    boxes = []
    for t in texts:
        try:
            boxes.append((t, t.get_window_extent(renderer)))
        except Exception:
            continue
    overlaps = []
    for i, (a, ba) in enumerate(boxes):
        for b, bb in boxes[i + 1:]:
            if ba.overlaps(bb):
                # shrink slightly: touching boxes of stacked lines are not overlaps
                if (min(ba.x1, bb.x1) - max(ba.x0, bb.x0)) > 1.5 and (min(ba.y1, bb.y1) - max(ba.y0, bb.y0)) > 1.5:
                    overlaps.append((a.get_text(), b.get_text()))
    fig_box = fig.bbox
    outside = [t.get_text() for t, bb in boxes
               if bb.x0 < fig_box.x0 - 1 or bb.y0 < fig_box.y0 - 1 or bb.x1 > fig_box.x1 + 1 or bb.y1 > fig_box.y1 + 1]
    return overlaps, outside


def legend_below(ax, handles, title=None, max_cols=6, fontsize=None, gap_pt=6):
    """
    Legend in compact columns under the axes (below the x-axis label), so the plot keeps the full
    figure width. Uses the most columns (up to max_cols) whose total width fits the figure width.
    The canvas grows downwards when saved (tight bounding box), so the plot itself is not squeezed.
    """
    fig = ax.figure
    fs = fontsize or font_size("small")
    fig.canvas.draw()
    r = fig.canvas.get_renderer()
    bb = ax.get_tightbbox(r).transformed(ax.transAxes.inverted())
    y = bb.y0 - gap_pt / (ax.get_position().height * fig.get_figheight() * 72)
    fig_w = fig.get_figwidth() * fig.dpi
    leg = None
    for ncol in range(min(max_cols, len(handles)), 0, -1):
        if leg is not None:
            leg.remove()
        leg = ax.legend(handles=handles, title=title, loc="upper left", bbox_to_anchor=(0, y), ncol=ncol,
                        fontsize=fs, title_fontsize=fs, alignment="left", borderaxespad=0, frameon=False,
                        handlelength=1.0, handletextpad=0.5, columnspacing=1.4, labelspacing=0.35)
        fig.canvas.draw()
        if leg.get_window_extent(r).width <= fig_w * 0.98:
            break
    return leg


def save_figure(fig, path, tight=True, message=None, close=True, dpi=None):
    """
    Saves a figure in every configured format (default PNG at 300 dpi).

    path    : output path; its extension is replaced per format
    message : text for the [INFO] line (default: 'Figure')
    Returns the list of saved paths.
    """
    stem = os.path.splitext(path)[0]
    saved = []
    if os.environ.get("MESHLYTICS_FIG_CHECK") == "1":
        overlaps, outside = find_text_overlaps(fig)
        if tight:
            outside = []  # tight saving grows the canvas to include every text
        if overlaps or outside:
            print(f"[CHECK] {os.path.basename(stem)}: {len(overlaps)} text overlaps, {len(outside)} texts outside the canvas")
            for a, b in overlaps[:5]:
                print(f"[CHECK]   overlap: '{a[:40]}' / '{b[:40]}'")
    for ext in FIGURE_SETTINGS["formats"]:
        out = f"{stem}.{ext}"
        if os.name == "nt" and len(os.path.abspath(out)) >= 250 and not out.startswith("\\\\?\\"):
            out = "\\\\?\\" + os.path.abspath(out)   # Windows long-path form (paths over 260 characters)
        kwargs = {"dpi": dpi or FIGURE_SETTINGS["dpi"]} if ext == "png" else {}
        if tight:
            kwargs.update(bbox_inches="tight", pad_inches=0.15)
        fig.savefig(out, **kwargs)
        saved.append(out)
    if close:
        plt.close(fig)
    print(f"[INFO] {message or 'Figure'} saved to: {saved[0]}" + (f" (+{', '.join(os.path.splitext(s)[1] for s in saved[1:])})" if len(saved) > 1 else ""))
    return saved


apply_theme()
