#!/usr/bin/env python3
"""Read-only adaptation of OrangeFox keymaps and image-backed tablet keyboards.

TB522FU's classic landscape-HDPI reference uses 192/96/288/960-pixel
standard/half/shift/space keys on a 1920-pixel row. The portrait theme's
108/54/162/540 proportions are identical; round cumulative boundaries, not
individual widths. Fonts and glyph resources are deliberately left alone.
"""

from __future__ import annotations

import copy
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path, PurePosixPath

__all__ = ["adapt_keyboard_theme"]

_SVG_NS = "http://www.w3.org/2000/svg"
_XLINK_NS = "http://www.w3.org/1999/xlink"
_KEY = re.compile(r"key(\d{2})\Z")
_LONG = re.compile(r"long(\d{2})\Z")
_PREFIX = re.compile(r"(\d+):(.*)\Z", re.DOTALL)
_PAINT = (
    "fill", "fill-opacity", "fill-rule", "stroke", "stroke-opacity",
    "stroke-width", "stroke-linecap", "stroke-linejoin", "stroke-miterlimit",
    "stroke-dasharray", "stroke-dashoffset", "color", "visibility", "display",
    "filter", "clip-path", "mask", "class",
)


@dataclass(frozen=True)
class _Cell:
    left: int
    right: int
    old_left: int
    old_right: int
    old_total: int
    key: str


def _key_parts(value: str, default: int) -> tuple[int, str]:
    if len(value) == 1:  # A literal colon or space is also a single key.
        return default, value
    match = _PREFIX.fullmatch(value)
    if not match:
        raise ValueError(f"invalid keyboard key definition: {value!r}")
    return int(match[1]), match[2]


def _layouts(keyboard: ET.Element) -> list[ET.Element]:
    return [node for node in keyboard if re.fullmatch(r"layout\d+", str(node.tag))]


def _adapt_layout(layout: ET.Element, width: int, height: int, numeric: bool):
    keysize = layout.find("keysize")
    if keysize is None:
        raise ValueError("keyboard layout has no keysize")
    default = int(keysize.get("width", "0"))
    rows = [node for node in layout if re.fullmatch(r"row\d+", str(node.tag))]
    if not rows:
        raise ValueError("keyboard layout has no rows")
    if [row.tag for row in rows] != [f"row{i}" for i in range(1, len(rows) + 1)]:
        raise ValueError("keyboard rows must be consecutive")
    geometry = []
    for row in rows:
        names = sorted(name for name in row.attrib if _KEY.fullmatch(name))
        if names != [f"key{i:02d}" for i in range(1, len(names) + 1)]:
            raise ValueError("keyboard keys must be consecutive")
        entries = []
        old_left = 0
        for name in names:
            old_width, key = _key_parts(row.get(name), default)
            if old_width <= 0:
                raise ValueError("keyboard key widths must be positive")
            entries.append((name, old_width, key, old_left))
            old_left += old_width
        old_total = old_left
        if numeric:
            while entries and not entries[0][2] and not row.get("long" + entries[0][0][3:]):
                entries.pop(0)
            while entries and not entries[-1][2] and not row.get("long" + entries[-1][0][3:]):
                entries.pop()
            if len(entries) != 3 or any(not entry[2] for entry in entries):
                raise ValueError("numeric rows must contain three active keys after edge removal")
        total = sum(entry[1] for entry in entries)
        if not total:
            raise ValueError("keyboard row has no keys")
        attrs = {name: value for name, value in row.attrib.items()
                 if not (_KEY.fullmatch(name) or _LONG.fullmatch(name))}
        cells = []
        left = cumulative = 0
        for index, (old_name, old_width, key, old_x) in enumerate(entries, 1):
            cumulative += old_width
            right = round(Fraction(width * cumulative, total))
            if right <= left:
                raise ValueError("target width is too small for the keyboard row")
            attrs[f"key{index:02d}"] = f"{right - left}:{key}"
            longpress = row.get("long" + old_name[3:])
            if longpress is not None:
                attrs[f"long{index:02d}"] = longpress
            cells.append(_Cell(left, right, old_x, old_x + old_width, old_total, key))
            left = right
        row.attrib.clear()
        row.attrib.update(attrs)
        geometry.append(cells)
        if len(geometry) == 1:
            keysize.set("width", str(max(1, round(Fraction(width * default, total)))))
    keysize.set("height", str(height))
    return geometry


def _adapt_labels(parent: ET.Element, rows, width: int):
    for label in parent.findall("keylabel"):
        match = _PREFIX.fullmatch(label.get("key", ""))
        if not match or int(match[1]) == 0:
            continue
        cells = [cell for row in rows for cell in row if cell.key == match[2]]
        new_width = (cells[0].right - cells[0].left if cells else
                     round(Fraction(width * int(match[1]), rows[0][0].old_total)))
        label.set("key", f"{new_width}:{match[2]}")


def _canvas(svg: ET.Element) -> tuple[float, float]:
    if svg.tag.rsplit("}", 1)[-1] != "svg":
        raise ValueError("keyboard background is not an SVG")
    if svg.get("viewBox"):
        box = [float(value) for value in re.split(r"[ ,]+", svg.get("viewBox").strip())]
        if len(box) != 4 or box[:2] != [0, 0] or min(box[2:]) <= 0:
            raise ValueError("keyboard SVG requires an origin-zero viewBox")
        return box[2], box[3]
    return tuple(float(svg.get(name, "0").removesuffix("px")) for name in ("width", "height"))


def _number(value: float) -> str:
    return format(value, ".12g")


def _presentation(node: ET.Element, inherited: dict[str, str]) -> dict[str, str]:
    inline = dict(part.split(":", 1) for part in node.get("style", "").split(";") if ":" in part)
    inline = {name.strip(): value.strip() for name, value in inline.items()}
    paint = inherited.copy()
    for name in _PAINT:
        if name in node.attrib:
            paint[name] = node.get(name)
        if name in inline:
            paint[name] = inline[name]
    opacity = inline.get("opacity", node.get("opacity"))
    if opacity is not None:
        paint["opacity"] = _number(float(inherited.get("opacity", "1")) * float(opacity))
    return paint


def _kind(cell: _Cell, row: int, row_count: int) -> str:
    return "other" if (cell.key in ("action", "c:8") or cell.key.startswith("layout") or
                       (row == row_count - 1 and cell.key in ("/", "."))) else "normal"


def _rebuild_svg(text: str, rows, width: int, row_height: int) -> str:
    original = ET.fromstring(text)
    source_w, source_h = _canvas(original)
    if min(source_w, source_h) <= 0:
        raise ValueError("keyboard SVG canvas must be positive")
    height = len(rows) * row_height
    svg = ET.Element("svg", {"xmlns": _SVG_NS, "version": "1.1", "width": str(width),
                             "height": str(height), "viewBox": f"0 0 {width} {height}"})
    backgrounds, faces, shadows = [], [], []

    def collect(node, inherited):
        tag = str(node.tag).rsplit("}", 1)[-1]
        if tag in ("defs", "metadata", "namedview", "style", "title", "desc"):
            return
        paint = _presentation(node, inherited)
        if tag == "rect":
            box = tuple(float(node.get(name, "0")) for name in ("x", "y", "width", "height"))
            if abs(box[2] - source_w) < 0.5 and abs(box[3] - source_h) < 0.5:
                backgrounds.append(paint)
            elif min(box[2:]) > 0:
                faces.append((node, box, paint))
        elif tag == "path":
            shadows.append(paint)
        for child in node:
            collect(child, paint)

    collect(original, {})
    for child in original:
        tag = str(child.tag).rsplit("}", 1)[-1]
        if tag not in ("defs", "style", "title", "desc"):
            continue
        definition = copy.deepcopy(child)
        # Unprefixed SVG tags are required by the recovery's NanoSVG parser.
        # Avoid ElementTree's process-global namespace registration entirely.
        for node in definition.iter():
            if str(node.tag).startswith("{" + _SVG_NS + "}"):
                node.tag = node.tag.split("}", 1)[1]
            for name in list(node.attrib):
                if name.startswith("{" + _XLINK_NS + "}"):
                    node.set("xlink:" + name.split("}", 1)[1], node.attrib.pop(name))
                    svg.set("xmlns:xlink", _XLINK_NS)
        svg.append(definition)
    for index, paint in enumerate(backgrounds):
        ET.SubElement(svg, "rect", {**paint, "id": f"keyboard-bg-{index}", "x": "0", "y": "0",
                                    "width": str(width), "height": str(height), "rx": "0", "ry": "0"})
    if not faces:
        if shadows:
            raise ValueError("path-only keyboard backgrounds cannot be identified as rounded keys")
        return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(svg, encoding="unicode") + "\n"

    prototypes = {}
    for node, box, paint in faces:
        row_index = min(len(rows) - 1, max(0, int((box[1] + box[3] / 2) * len(rows) / source_h)))
        candidates = [(index, cell) for index, cell in enumerate(rows[row_index]) if cell.key]
        index, _ = min(candidates, key=lambda item: abs(
            (item[1].old_left + item[1].old_right) * source_w / (2 * item[1].old_total) -
            (box[0] + box[2] / 2)))
        prototypes[(row_index, index)] = (node, paint)
    shadow_group = ET.SubElement(svg, "g", {"id": "key-shadows"}) if shadows else None
    key_group = ET.SubElement(svg, "g", {"id": "keycaps"})
    for row_index, row in enumerate(rows):
        for index, cell in enumerate(row):
            if not cell.key:
                continue
            prototype = prototypes.get((row_index, index))
            if prototype is None:
                # Old enter keycaps are baked into separate glyph resources.
                # Supply a full cell using the matching functional-key palette.
                candidates = [position for position in prototypes if
                              _kind(rows[position[0]][position[1]], position[0], len(rows)) ==
                              _kind(cell, row_index, len(rows))]
                position = min(candidates or list(prototypes), key=lambda position: (
                    abs((rows[position[0]][position[1]].old_right - rows[position[0]][position[1]].old_left) -
                        (cell.old_right - cell.old_left)), abs(position[0] - row_index), abs(position[1] - index)))
                prototype = prototypes[position]
            node, paint = prototype
            inset_x = min(8, (cell.right - cell.left - 1) / 2)
            inset_y = min(8, (row_height - 1) / 4)
            depth = min(4, (row_height - 1) / 4) if shadows else 0
            key_w = cell.right - cell.left - 2 * inset_x
            key_h = row_height - 2 * inset_y - depth
            rx = float(node.get("rx", node.get("ry", "0")))
            ry = float(node.get("ry", node.get("rx", "0")))
            geometry = {"x": _number(cell.left + inset_x), "y": _number(row_index * row_height + inset_y),
                        "width": _number(key_w), "height": _number(key_h),
                        "rx": _number(min(rx, key_w / 2)), "ry": _number(min(ry, key_h / 2))}
            if shadow_group is not None:
                ET.SubElement(shadow_group, "rect", {**shadows[0], **geometry,
                              "y": _number(row_index * row_height + inset_y + depth),
                              "id": f"shadow-r{row_index + 1}-k{index + 1}"})
            ET.SubElement(key_group, "rect", {**paint, **geometry, "id": f"key-r{row_index + 1}-k{index + 1}"})
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(svg, encoding="unicode") + "\n"


def adapt_keyboard_theme(theme_path, width=2944, row_height=192) -> tuple[str, dict[str, str]]:
    """Return adapted keyboard XML and theme-relative SVG texts, without writes.

    ``theme_path`` accepts a string or pathlib.Path and names the source theme
    directory. Returned SVG keys include ``images/``, use forward slashes, and
    are safe to join to the destination theme. Main keyboards have four rows
    (768 pixels by default); terminal/number bars remain 96/116 pixels high.
    The caller must update keyboard/terminal Y variables and list viewports,
    and commit all returned texts transactionally. No generator is invoked.
    """
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0
           for value in (width, row_height)):
        raise ValueError("width and row_height must be positive integers")
    theme = Path(theme_path)
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    keyboard = ET.fromstring((theme / "pages/templates/keyboard.xml").read_text(encoding="utf-8"), parser)
    geometry = {}
    for template in keyboard.findall("./templates/template"):
        for node in template.findall("keyboard"):
            image = node.find("layout")
            role = image.get("resource") if image is not None else None
            layouts = _layouts(node)
            if not layouts:
                raise ValueError("keyboard has no layouts")
            main = role in ("keyback", "keyback_num")
            if main and any(len(layout.findall("keysize")) != 1 for layout in layouts):
                raise ValueError("main keyboard must have one keysize per layout")
            placement = node.find("placement")
            if placement is None:
                raise ValueError("keyboard has no placement")
            height = row_height if main else (96 if template.get("name") == "keyboardterminaltemplate" else 116)
            adapted = []
            for layout in layouts:
                rows = _adapt_layout(layout, width, height, role == "keyback_num")
                if main and len(rows) != 4:
                    raise ValueError("image-backed main keyboard must have four rows")
                _adapt_labels(layout, rows, width)
                adapted.extend(rows)
                if main:
                    boundaries = [[(cell.left, cell.right, bool(cell.key)) for cell in row] for row in rows]
                    if role in geometry and boundaries != [[(cell.left, cell.right, bool(cell.key)) for cell in row]
                                                          for row in geometry[role]]:
                        raise ValueError("image-backed keyboard layouts must share key boundaries")
                    geometry.setdefault(role, rows)
            _adapt_labels(node, adapted, width)
            placement.set("x", "0")
            placement.set("w", str(width))
            placement.set("h", str(4 * row_height if main else height))

    resources = {}
    texts = {}
    for directory in ("themes", "resources"):
        for path in sorted((theme / directory).rglob("*.xml")):
            text = path.read_text(encoding="utf-8")
            # TWRP accepts non-XML attribute names (for example states 100="").
            # Parse only image declarations, not unrelated permissive markup.
            text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
            for declaration in re.finditer(r"<image\b[^>]*>", text):
                image = ET.fromstring(declaration[0][:-1].rstrip().rstrip("/") + "/>")
                role = image.get("name")
                if role not in geometry:
                    continue
                filename = PurePosixPath(image.get("filename", "").replace("\\", "/"))
                if filename.is_absolute() or ".." in filename.parts or ":" in str(filename):
                    raise ValueError("keyboard SVG resource must be theme-relative")
                if filename.suffix.lower() != ".svg":
                    continue  # Never reauthor PNG glyphs or raster resources.
                relative = filename if filename.parts[0] == "images" else PurePosixPath("images") / filename
                relative = str(relative)
                if relative in resources and resources[relative] != role:
                    raise ValueError("one SVG cannot back both alpha and numeric keymaps")
                resources[relative] = role
    for path in sorted((theme / "images/SVG/Keyboard").glob("*.svg")):
        relative = path.relative_to(theme).as_posix()
        if relative in resources:
            continue
        text = path.read_text(encoding="utf-8")
        if _canvas(ET.fromstring(text)) == (1080, 645):
            role = "keyback_num" if "num" in path.stem.lower() else "keyback"
            if role in geometry:
                resources[relative] = role
                texts[relative] = text
    svg_texts = {}
    for relative, role in sorted(resources.items()):
        text = texts.get(relative)
        if text is None:
            text = (theme / relative).read_text(encoding="utf-8")
        svg_texts[relative] = _rebuild_svg(text, geometry[role], width, row_height)
    xml_text = '<?xml version="1.0"?>\n' + ET.tostring(keyboard, encoding="unicode") + "\n"
    return xml_text, svg_texts
