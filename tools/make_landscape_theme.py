#!/usr/bin/env python3
"""Author the built-in 2944x1840 horizontal OrangeFox resources.

Uses the TB522FU's landscape_hdpi page organization, not a stretched or
centered portrait viewport. Run before building recovery; no runtime ui.zip
is needed. Existing generated resources are backed up before replacement.
"""

import argparse
import copy
import math
import re
import shutil
import struct
import tempfile
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

PANEL_W, PANEL_H = 2944, 1840
OLD_W = 1080
DELTA = PANEL_W - OLD_W
MARGIN = 48
CONTENT_W = PANEL_W - 2 * MARGIN
CENTER = PANEL_W // 2
COLUMN_W = (CONTENT_W - MARGIN) // 2
RIGHT_X = MARGIN + COLUMN_W + MARGIN
KEYBOARD_H = 768
SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)


def parse_xml(text):
    # TinyXML permits numeric battery-state attributes; ElementTree does not.
    def numeric_attributes(tag):
        return re.sub(r'([^\s=]+)(\s*=\s*)("[^"]*"|\'[^\']*\')',
                      lambda match: ("__numeric_attribute_" + match[1] if match[1].isdigit() else match[1])
                      + match[2] + match[3], tag[0])
    text = re.sub(r'<[A-Za-z_][\w:.-]*(?:[^\'">]|"[^"]*"|\'[^\']*\')*>', numeric_attributes, text)
    return ET.fromstring(text, parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)))


def xml_bytes(root):
    ET.indent(root, space="\t")
    text = ET.tostring(root, encoding="unicode")
    def restore_numeric_attributes(tag):
        return re.sub(r'([^\s=]+)(\s*=\s*)("[^"]*"|\'[^\']*\')',
                      lambda match: re.sub(r'^__numeric_attribute_(\d+)$', r'\1', match[1])
                      + match[2] + match[3], tag[0])
    text = re.sub(r'<[A-Za-z_][\w:.-]*(?:[^\'">]|"[^"]*"|\'[^\']*\')*>', restore_numeric_attributes, text)
    return ('<?xml version="1.0" encoding="utf-8"?>\n' + text + "\n").encode()


def parse_layout_files(files):
    trees = {}
    for name, data in files.items():
        if not name.endswith(".xml") or name.startswith("themes/sed/"):
            continue
        try:
            trees[name] = parse_xml(data.decode("utf-8"))
        except ET.ParseError as error:
            raise ValueError(f"layout XML {name}: {error}") from error
    return trees


def layout_variables(root):
    nodes = {node.get("name"): node for node in root.findall("./variables/variable")}
    updates = {
        "screen_w": PANEL_W, "center_x": CENTER, "content_w": CONTENT_W,
        "input_w": CONTENT_W, "input_backup_w": CONTENT_W - 79,
        "input_encr_width": 696 + DELTA, "slidervalue_w": CONTENT_W,
        "slider_x": MARGIN,
        "console_width": CONTENT_W, "terminal_width": CONTENT_W,
        "btn_w": COLUMN_W, "btn_half_w": (CONTENT_W - 3 * MARGIN) // 4,
        "col1_x_neg": PANEL_W - MARGIN, "col2_x": RIGHT_X,
        "col2_x_text": RIGHT_X + 48, "tab_w": PANEL_W // 3 - 48,
        "nav_panel_x": MARGIN, "nav_panel_full_h": 360, "nav_item_w": CONTENT_W // 4,
        "row_navbtn_w": PANEL_W // 3,
        "back_button_x": round(PANEL_W / 6), "slideout_button_x": round(PANEL_W * 5 / 6),
        "keyboard_y": "%screen_h%-912", "keyboard_num_y": "%screen_h%-1028",
        "keyboard_terminal_y": "%screen_h%-1008", "keyboard_term_num_y": "%screen_h%-1124",
        "fm_search_h": "%screen_min_cut%-1152", "fm_search_num_h": "%screen_min_cut%-1260",
        "terminal_height": "%screen_min_cut%-1144", "terminal_num_height": "%screen_min_cut%-1260",
        "adv_additions_h": "%screen_min_cut%-1404",
        "ota_update_progress_x": CENTER - 214,
    }
    right_names = (
        "status_right_x", "battery_12_x", "battery_24_x", "battery_2_12_x", "battery_2_24_x",
        "ab_btn1_x", "ab_btn2_x", "ab_btn3_x", "ab_btn01_x", "ab_btn02_x", "ab_menu_x",
        "ab_menu_sort_x", "ab_menu_sort_text_x", "btn_float_x", "main_switch_icon_x",
        "btn_raised_right_x", "snackbar_button_x", "snackbar_button_2_x", "card1_mtag_x", "card1_ctag_x",
    )
    for name in right_names:
        value = nodes[name].get("value")
        match = re.match(r"^(\d+)(.*)$", value)
        if not match:
            raise ValueError("right anchor is not numeric: " + name)
        updates[name] = str(int(match[1]) + DELTA) + match[2]
    for index in range(1, 5):
        center = MARGIN + (index * 2 - 1) * CONTENT_W // 8
        updates[f"nav_item_{index}"] = center
        updates[f"np_pill_x{index}"] = center - 96
        updates[f"ws_cards_x{index}"] = center
    for suffix, center in zip(("r", "w", "x"), (round(PANEL_W / 6), CENTER, round(PANEL_W * 5 / 6))):
        updates["ch_" + suffix + "_x"] = center - 48
        updates["txt_" + suffix + "_x"] = center - 104
        updates["chtxt_" + suffix + "_x"] = center
    for name in ("bg_storage_x", "bg_storage_hdr_x", "storage_indent_x", "db_left_x",
                 "db_right_x", "pattern_x"):
        updates[name] = int(nodes[name].get("value")) + DELTA // 2
    for name, value in updates.items():
        if name not in nodes:
            raise ValueError("required layout variable missing: " + name)
        nodes[name].set("value", str(value))
    additions = {
        "ls_right_x": RIGHT_X, "ls_right_caption_x": RIGHT_X + 12,
        "ls_column_w": COLUMN_W, "ls_left_center": MARGIN + COLUMN_W // 2,
        "ls_right_center": RIGHT_X + COLUMN_W // 2,
        "ls_input_indent_w": PANEL_W - 216 - MARGIN,
        "ls_input_backup_indent_w": PANEL_W - 216 - MARGIN - 79,
        "ls_nav_column_h": "%screen_min_cut%-714",
        "ls_file_browser_w": 1800, "ls_file_tools_x": 1851, "ls_file_tools_w": 1090,
        "ls_file_center": MARGIN + 1800 // 2,
        "ls_file_tools_h": "%screen_min_cut%-722",
        "ls_backup_parts_h": "%screen_min_cut%-1272",
        "ls_backup_options_h": "%lb_group_l4%",
        "ls_restore_parts_h": "%screen_min_cut%-1043",
        "ls_flash_parts_h": "%screen_min_cut%-1043",
    }
    container = root.find("variables")
    for name, value in additions.items():
        ET.SubElement(container, "variable", {"name": name, "value": str(value)})


def adapt_placements(root, modal=False):
    for parent in root.iter():
        if not isinstance(parent.tag, str):
            continue
        placement = parent.find("placement")
        if placement is None:
            continue
        original_x = placement.get("x", "0")
        original_w = placement.get("w", "")
        x = int(original_x) if original_x.isdigit() else None
        width = int(original_w) if original_w.isdigit() else None
        style = parent.get("style", "")
        if style in ("tab_btn", "tab_btn_hl") and x is not None and width == 360:
            if x in (156, 564):
                index = (0, 1)[x == 564]
                left, right = round(index * PANEL_W / 2), round((index + 1) * PANEL_W / 2)
            else:
                index = x // 360
                left, right = round(index * PANEL_W / 3), round((index + 1) * PANEL_W / 3)
            placement.set("x", str(left))
            placement.set("w", str(right - left))
            continue
        if style == "tab" and x is not None:
            if x in (180, 588):
                placement.set("x", str(round((0 if x == 180 else 1) * PANEL_W / 2) + 24))
                image = parent.find("image")
                if image is None:
                    image = ET.SubElement(parent, "image")
                image.set("resource", "ls_tab_hl_two")
            else:
                placement.set("x", str(round((x - 24) / 360 * PANEL_W / 3) + 24))
            continue
        if x is not None:
            if modal and not (x == 0 and original_w in ("%screen_w%", "1080")):
                new_x = x + DELTA // 2
            elif x == 540:
                new_x = CENTER
            elif x >= 708:
                new_x = x + DELTA
            elif 308 < x < 708:
                new_x = x + DELTA // 2
            else:
                new_x = x
            placement.set("x", str(new_x))
            if width is not None and width >= 680 and x + width >= 984 and not modal:
                placement.set("w", str(PANEL_W - (OLD_W - x - width) - new_x))
        if original_w == "1080" and original_x == "0":
            placement.set("w", "%screen_w%")
        if parent.tag == "partitionlist" and original_x == "0" and original_w in ("%screen_w%", "1080") and not modal:
            place(parent, x=MARGIN, w=CONTENT_W)
        if original_x in ("%col1_x_indent%", "%col1_x_caption%"):
            if original_w == "%input_w%":
                placement.set("w", "%ls_input_indent_w%")
            elif original_w == "%input_backup_w%":
                placement.set("w", "%ls_input_backup_indent_w%")
        if parent.tag == "input" and parent.find("data") is not None and not modal:
            data_name = parent.find("data").get("name", "")
            if data_name in ("tw_file_location1_tmp", "location_input_tmp"):
                placement.set("w", "%ls_input_indent_w%")
        if parent.tag == "image":
            image = parent.find("image")
            if image is not None and image.get("resource") == "empty_folder" and original_x == "304":
                placement.set("x", "%center_x%")
                placement.set("placement", "5")


def place(node, **attributes):
    placement = node.find("placement")
    if placement is None:
        raise ValueError("placement missing from " + str(node.tag))
    for name, value in attributes.items():
        placement.set(name, str(value))


def reflow_pages(trees):
    pages = {page.get("name"): page for tree in trees.values() for page in tree.findall("./pages/page")}
    reboot = pages["reboot"]
    power_width = (CONTENT_W - 2 * MARGIN) // 3
    power_lefts = (MARGIN, (PANEL_W - power_width) // 2, PANEL_W - MARGIN - power_width)
    power_conditions = ("tw_reboot_poweroff", "tw_reboot_system", "tw_reboot_recovery")
    cards = reboot.findall("button[@style='power_card']")
    if len(cards) != 3:
        raise ValueError("expected three reboot cards")
    for card, left in zip(cards, power_lefts):
        place(card, x=left)
    for node in reboot:
        if node.tag not in ("image", "text") or node.find("placement") is None:
            continue
        for condition, left in zip(power_conditions, power_lefts):
            if node.find(f"condition[@var1='{condition}']") is not None:
                place(node, x=left + power_width // 2)
    reboot_lists = reboot.findall("listbox")
    if len(reboot_lists) != 2:
        raise ValueError("expected reboot destinations and slot controls")
    place(reboot_lists[0], x=MARGIN, w=CONTENT_W, h="%bl_h3%")
    place(reboot_lists[1], x=MARGIN, w=CONTENT_W, y="%row5_1_y%", h="%bl_h2%")
    slot_caption = reboot.find("text[@style='caption']")
    place(slot_caption, x=MARGIN + 12, y="%row4_3a_y%")
    for partitionlist in pages["mount"].findall("partitionlist"):
        place(partitionlist, x=MARGIN, w=CONTENT_W)

    # Portrait slot options sit below the tablet's swipe area. Keep their
    # feature/partition conditions intact and give them a visible second pane.
    flash = pages["flash_image_confirm"]
    partitions, flash_options = flash.findall("partitionlist"), flash.findall("listbox")
    if len(partitions) != 1 or len(flash_options) != 1 or len(flash_options[0].findall("listitem")) != 1:
        raise ValueError("flash image partition/options structure changed")
    place(partitions[0], x=MARGIN, y="%row2_2a_y%", w=COLUMN_W, h="%ls_flash_parts_h%")
    place(flash_options[0], x=RIGHT_X, y="%row2_2a_y%", w=COLUMN_W, h="%lb_group_l1%")
    flash_options[0].set("style", "settingslist_group")
    flash_captions = [node for node in flash.findall("text[@style='caption']") if node.findtext("text") == "{@options_cptn}"]
    if len(flash_captions) != 1:
        raise ValueError("flash image options caption changed")
    place(flash_captions[0], x=RIGHT_X + 12, y="%row2_1a_y%", placement=0)
    # The portrait caption lacks the option's VAB guard and can label an empty pane.
    vab_condition = flash_options[0].find("condition[@var1='fox_vab_device']")
    if vab_condition is None:
        raise ValueError("flash image VAB option condition missing")
    if flash_captions[0].find("condition[@var1='fox_vab_device']") is None:
        flash_captions[0].append(copy.deepcopy(vab_condition))

    advanced = pages["advanced"]
    boxes = advanced.findall("listbox")
    if len(boxes) != 2:
        raise ValueError("advanced page changed; expected main/additions groups")
    for index, box in enumerate(boxes):
        place(box, x=MARGIN if index == 0 else RIGHT_X, y="%row1_1a_y%", w=COLUMN_W, h="%ls_nav_column_h%")
    captions = [node for node in advanced.findall("text") if node.get("style") == "caption"]
    for index, caption in enumerate(captions):
        place(caption, x=MARGIN + 45 if index == 0 else RIGHT_X + 45, y="%row0_a_y%")

    # The extended OF file selector handles folder holds and batch selection.
    # Keep it intact; use the tablet's remaining column for the existing tools.
    browser = pages["filemanagerlist"]
    for selector in browser.findall("fileselector"):
        place(selector, x=MARGIN, w="%ls_file_browser_w%")
    for node in browser:
        if node.find("condition[@var1='of_empty_dir']") is not None and node.find("placement") is not None:
            node.find("placement").set("x", "%ls_file_center%")
    menu_boxes = pages["menu"].findall("listbox")
    if len(menu_boxes) != 2:
        raise ValueError("file tools menu changed")
    for index, original in enumerate(menu_boxes):
        sidebar = copy.deepcopy(original)
        place(sidebar, x="%ls_file_tools_x%", w="%ls_file_tools_w%",
              y="%search_files_y%" if index == 0 else "%row4_1_y%",
              h="%lb_group_l3%" if index == 0 else "%bl_h4%")
        browser.append(sidebar)

    # The sidebar replaces only this header menu, not other menu/back routes.
    menu_buttons = [node for node in browser.findall("button[@style='actionbar']")
                    if node.findtext("action[@function='page']") == "menu"
                    and node.find("placement[@y='%ab_search_y%']") is not None]
    menu_icons = [node for node in browser.findall("image")
                  if node.find("image[@resource='actionbar_menu']") is not None
                  and node.find("placement[@y='%ab_search_y%']") is not None]
    if len(menu_buttons) != 1 or len(menu_icons) != 1:
        raise ValueError("Files header menu structure changed")
    menu_button, menu_icon = menu_buttons[0], menu_icons[0]
    if (menu_button.find("placement").attrib != menu_icon.find("placement").attrib
            or [(action.attrib, action.text) for node in (menu_button, menu_icon)
                for action in node.iter("action")] != [({"function": "page"}, "menu")]
            or any(node.find(".//condition") is not None for node in (menu_button, menu_icon))):
        raise ValueError("Files header menu route changed")
    browser.remove(menu_button)
    browser.remove(menu_icon)

    sort_buttons = [node for node in browser.findall("button[@style='actionbar']")
                    if node.findtext("action[@function='overlay']") == "fm_sort"
                    and node.find("placement[@y='%ab_search_y%']") is not None]
    sort_icons = [node for node in browser.findall("image")
                  if node.find("image[@resource='actionbar_sort']") is not None
                  and node.find("placement[@y='%ab_search_y%']") is not None]
    if (len(sort_buttons) != 1 or len(sort_icons) != 1
            or sort_buttons[0].find("placement").attrib != sort_icons[0].find("placement").attrib):
        raise ValueError("Files sort control structure changed")
    for node in sort_buttons + sort_icons:
        place(node, x="%ab_btn1_x%")

    # Static option menus can form two columns without changing any item action.
    for name in ("settings", "ext_custom", "fox_other_additions", "filemanageroptions"):
        page = pages.get(name)
        if page is None:
            continue
        boxes = page.findall("listbox")
        if len(boxes) != 1 or len(boxes[0].findall("listitem")) < 6:
            continue
        original = boxes[0]
        items = original.findall("listitem")
        halfway = math.ceil(len(items) / 2)
        if name == "settings":
            # Security/OTA each have mutually exclusive variants. Count them
            # as one row so hidden alternatives do not unbalance the columns.
            row_names = list(dict.fromkeys(item.get("name") for item in items))
            left_names = set(row_names[:math.ceil(len(row_names) / 2)])
            halfway = next((index for index, item in enumerate(items)
                            if item.get("name") not in left_names), len(items))
        second = copy.deepcopy(original)
        for item in original.findall("listitem")[halfway:]:
            original.remove(item)
        for item in second.findall("listitem")[:halfway]:
            second.remove(item)
        place(original, x=MARGIN, w=COLUMN_W)
        place(second, x=RIGHT_X, w=COLUMN_W)
        page.insert(list(page).index(original) + 1, second)

    for name in ("ext_custom_fonts", "ext_custom_fonts_sec"):
        for index, box in enumerate(pages[name].findall("listbox")):
            place(box, x=MARGIN if index % 2 == 0 else RIGHT_X, w=COLUMN_W,
                  y=("%row2_1_y%", "%row2_2a_y%", "%row3_1_y%", "%row3_2a_y%")[index // 2])

    # All twelve accent choices fit in two horizontal rows rather than three
    # portrait rows extending below navigation.
    workshop = pages["ext_custom_theme"]
    for node in workshop:
        placement = node.find("placement")
        if placement is None or placement.get("y") not in ("%ws_cards_y3%", "%ws_cards_y4%", "%ws_cards_y5%"):
            continue
        old_row = ("%ws_cards_y3%", "%ws_cards_y4%", "%ws_cards_y5%").index(placement.get("y"))
        old_col = int(re.search(r"x(\d+)", placement.get("x")).group(1)) - 1
        index = old_row * 4 + old_col
        placement.set("x", str(MARGIN + round((index % 6 + 0.5) * CONTENT_W / 6)))
        placement.set("y", "%ws_cards_y3%" if index < 6 else "%ws_cards_y4%")

    backup = pages["backup_prep"]
    for node in backup:
        placement = node.find("placement")
        if placement is None:
            continue
        if node.tag == "partitionlist":
            place(node, x=MARGIN, y="%row2_2a_y%", w=COLUMN_W, h="%ls_backup_parts_h%")
        elif node.tag == "text" and node.findtext("text") == "{@partitions}":
            place(node, x=MARGIN + 12, y="%row2_1a_y%")
        elif placement.get("y") in ("%row2_1a_y%", "%row2_2a_y%", "%row2_3a_y%"):
            place(node, x=RIGHT_X + (12 if node.tag == "text" else 0))
            if "w" in placement.attrib:
                placement.set("w", str(COLUMN_W))
    option_source = pages["ext_backup"].find("listbox[@style='settingslist_group']")
    if option_source is None:
        raise ValueError("backup options list missing")
    options = copy.deepcopy(option_source)
    place(options, x=RIGHT_X, y="%row3_2_y%", w=COLUMN_W, h="%ls_backup_options_h%")
    caption = ET.Element("text", {"style": "caption"})
    ET.SubElement(caption, "placement", {"x": str(RIGHT_X + 12), "y": "%row3_1_y%"})
    ET.SubElement(caption, "text").text = "{@options_hdr}"
    overlay = next(node for node in backup if node.find("fill[@color='%darktransparent%']") is not None)
    overlay_index = list(backup).index(overlay)
    backup.insert(overlay_index, caption)
    backup.insert(overlay_index + 1, options)

    # Grouped helpers must finish above even the optional number-bar keyboard.
    editor = pages["backupname"]
    for node in editor:
        placement = node.find("placement")
        if placement is None:
            continue
        if node.tag in ("input", "fill"):
            place(node, x=MARGIN, w=COLUMN_W - 96)
        elif node.tag == "button" and node.find("image[@resource='actionbar_clear']") is not None:
            place(node, x=MARGIN + COLUMN_W - 36)
    editor_lists = editor.findall("listbox")
    if len(editor_lists) != 2:
        raise ValueError("backup name helpers changed")
    for box, y in zip(editor_lists, ("%row1_1_y%", "%row2_3_y%")):
        place(box, x=RIGHT_X - 45, w=COLUMN_W + 90, y=y)

    restore = pages["restore_select"]
    for node in restore:
        placement = node.find("placement")
        if placement is None:
            continue
        if node.tag == "partitionlist":
            place(node, x=MARGIN, y="%row2_2a_y%", w=COLUMN_W, h="%ls_restore_parts_h%")
        elif node.tag == "text" and node.findtext("text") == "{@partitions}":
            place(node, x=MARGIN + 12, y="%row2_1a_y%")
        elif node.tag == "listbox":
            place(node, x=RIGHT_X, w=COLUMN_W)
        elif placement.get("y") in ("%row2_1a_y%", "%row2_2a_y%", "%row3_1a_y%"):
            place(node, x=RIGHT_X + 12)


def resize_png_band(data, width):
    """Resize only flat progress artwork, retaining its height/palette/pixels."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("progress resource is not PNG")
    chunks, offset, compressed = [], 8, bytearray()
    while offset < len(data):
        size = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + size]
        chunks.append((kind, payload))
        if kind == b"IDAT":
            compressed.extend(payload)
        offset += size + 12
    old_width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", chunks[0][1])
    if old_width <= 500:
        return data  # Small indicators/icons are not full-width progress bands.
    if (depth not in (1, 2, 4, 8, 16) or interlace != 0 or color not in (0, 2, 3, 4, 6)
            or (depth < 8 and color not in (0, 3)) or (depth == 16 and color == 3)):
        raise ValueError(f"unsupported progress PNG format: depth={depth}, color={color}, interlace={interlace}")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    bpp = max(1, (channels * depth + 7) // 8)
    stride = (old_width * channels * depth + 7) // 8
    raw = zlib.decompress(compressed)
    previous = bytearray(stride)
    output = bytearray()
    for y in range(height):
        mode = raw[y * (stride + 1)]
        row = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for x in range(stride):
            left, above = (row[x - bpp] if x >= bpp else 0), previous[x]
            corner = previous[x - bpp] if x >= bpp else 0
            if mode == 0:
                predictor = 0
            elif mode == 1:
                predictor = left
            elif mode == 2:
                predictor = above
            elif mode == 3:
                predictor = (left + above) // 2
            elif mode == 4:
                estimate = left + above - corner
                predictor = min((left, above, corner), key=lambda value: abs(estimate - value))
            else:
                raise ValueError("invalid PNG filter")
            row[x] = (row[x] + predictor) & 255
        output.append(0)
        for x in range(width):
            pixel = min(old_width - 1, x * old_width // width)
            if depth < 8:
                bit = pixel * depth
                sample = (row[bit // 8] >> (8 - depth - bit % 8)) & ((1 << depth) - 1)
                output.append(sample if color == 3 else sample * 255 // ((1 << depth) - 1))
            else:
                source = pixel * bpp
                output.extend(row[source:source + bpp])
        previous = row
    result = bytearray(data[:8])
    wrote_pixels = False
    for kind, payload in chunks:
        if kind == b"IHDR":
            payload = struct.pack(">IIBBBBB", width, height, max(8, depth), color, compression, filtering, interlace)
        elif kind == b"tRNS" and color == 0 and depth < 8:
            transparent = struct.unpack(">H", payload)[0] * 255 // ((1 << depth) - 1)
            payload = struct.pack(">H", transparent)
        elif kind == b"IDAT":
            if wrote_pixels:
                continue
            payload, wrote_pixels = zlib.compress(output, 9), True
        result.extend(struct.pack(">I", len(payload)) + kind + payload)
        result.extend(struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff))
    return bytes(result)


def build_landscape_files(source):
    from landscape_keyboard import adapt_keyboard_theme
    source = Path(source)
    files = {path.relative_to(source).as_posix(): path.read_bytes()
             for path in source.rglob("*") if path.is_file()}
    trees = parse_layout_files(files)
    resolution = trees["ui.xml"].find("details/resolution")
    if resolution is None or resolution.get("width") != "1080":
        raise ValueError("expected the known OrangeFox portrait source")
    resolution.attrib.update(width=str(PANEL_W), height=str(PANEL_H), resizing="1")
    for tree in trees.values():
        other_resolution = tree.find("details/resolution")
        if other_resolution is not None and other_resolution.get("width") == "1080":
            other_resolution.attrib.update(width=str(PANEL_W), height=str(PANEL_H), resizing="1")
            other_resolution.attrib.pop("h", None)
        for variable in tree.findall("./variables/variable"):
            if variable.get("name") == "screen_width":
                variable.set("value", str(PANEL_W))
            elif variable.get("name") == "screen_height":
                variable.set("value", str(PANEL_H))
    layout_variables(trees["resources/vars.xml"])
    for name, tree in trees.items():
        if name != "pages/templates/keyboard.xml":
            for page in tree.findall("./pages/page"):
                is_modal = any(node.get("name", "").startswith("dialog_body") for node in page.findall("template"))
                adapt_placements(page, modal=is_modal)
            for template in tree.findall("./templates/template"):
                adapt_placements(template)
    reflow_pages(trees)

    navbar = trees["pages/templates/navbar.xml"]
    for button in navbar.iter("button"):
        placement = button.find("placement")
        if placement is not None and placement.get("w") == "48" and placement.get("x") != "0":
            placement.set("x", "%col1_x_neg%")
        if placement is not None and placement.get("y") == "%ab_h%" and placement.get("h") == "%screen_h%":
            placement.set("h", "%gesture_basic_h%")
    images = trees["resources/images.xml"]
    shapes = {shape.get("name"): shape for shape in images.iter("shape")}
    shape_widths = {
        "power_card": str((CONTENT_W - 2 * MARGIN) // 3),
        "search_bar_bg": str(CONTENT_W),
        "main_switch_card_active": str(PANEL_W - 80),
        "main_switch_card_not_active": str(PANEL_W - 80),
        "nb_empty": "%row_navbtn_w%", "nb_hl": "%row_navbtn_w%",
        "nav_empty": "%nav_item_w%", "nav_hl": "%nav_item_w%", "tab_hl": "%tab_w%",
    }
    for name, width in shape_widths.items():
        if name not in shapes:
            raise ValueError("required horizontal resource missing: " + name)
        shapes[name].set("w", width)
    two_tab_shape = copy.deepcopy(shapes["tab_hl"])
    two_tab_shape.set("name", "ls_tab_hl_two")
    two_tab_shape.set("w", str(PANEL_W // 2 - 48))
    images.find("resources").append(two_tab_shape)
    for style in trees["resources/styles.xml"].iter("style"):
        if style.get("name") == "slidervalue":
            style.find("resource").attrib.pop("background", None)
    for name, tree in trees.items():
        files[name] = xml_bytes(tree)
    for name, data in list(files.items()):
        if name.startswith("themes/sed/") and name.endswith(".xml"):
            text = data.decode("utf-8")
            text = re.sub(r'<resolution\b[^>]*\bwidth="1080"[^>]*/>',
                          f'<resolution width="{PANEL_W}" height="{PANEL_H}" resizing="1"/>', text)
            for variable_name, value in (("screen_width", PANEL_W), ("screen_height", PANEL_H)):
                text = re.sub(r'(<variable\s+name="' + variable_name + r'"\s+value=")[^"]*(")',
                              lambda match: match[1] + str(value) + match[2], text)
            files[name] = text.replace('x="540"', f'x="{CENTER}"').encode()

    for name in ("images/SVG/Controls/slider_classic.svg", "images/SVG/Dialog/snackbar.svg"):
        svg = parse_xml(files[name].decode())
        height = 128 if "slider_classic" in name else 192
        svg.set("viewBox", f"0 0 {CONTENT_W if height == 128 else PANEL_W} {height}")
        for child in list(svg):
            svg.remove(child)
        ET.SubElement(svg, "{" + SVG_NS + "}rect", {
            "x": "0" if height == 128 else "24", "y": "0" if height == 128 else "24",
            "width": str(CONTENT_W if height == 128 else PANEL_W - 48),
            "height": "128" if height == 128 else "144", "rx": "64" if height == 128 else "30", "fill": "#000000"})
        files[name] = xml_bytes(svg)
    card_name = "images/SVG/About/card.svg"
    card = files[card_name].decode()
    card = card.replace("0 0 1080 336", f"0 0 {PANEL_W} 336")
    for old, new in (("1008.1543", "2872.1543"), ("1021.3645", "2885.3645"), ("1032", "2896")):
        card = card.replace(old, new)
    files[card_name] = card.encode()
    for name, data in list(files.items()):
        if "/Progress/" in name and name.endswith(".png") and "_small" not in name:
            files[name] = resize_png_band(data, PANEL_W)
    keyboard_xml, keyboard_svgs = adapt_keyboard_theme(source, PANEL_W, 192)
    files["pages/templates/keyboard.xml"] = keyboard_xml.encode()
    files.update({name: text.encode() for name, text in keyboard_svgs.items()})
    validate_landscape(files)
    return files


def validate_landscape(files):
    parsed = parse_layout_files(files)
    resolution = parsed["ui.xml"].find("details/resolution")
    if resolution.attrib != {"width": str(PANEL_W), "height": str(PANEL_H), "resizing": "1"}:
        raise ValueError("layout must use native unity-scale resolution")
    default_style = parsed.get("themes/style.xml")
    if default_style is None or default_style.find("./include/xml[@name='/twres/themes/styles/Dark.xml']") is None:
        raise ValueError("built-in style fallback must select the shipped Dark preset")
    dark_style = parsed.get("themes/styles/Dark.xml")
    if dark_style is None or dark_style.find("./variables/variable[@name='theme_style'][@value='Dark']") is None:
        raise ValueError("built-in Dark preset is missing")
    values = {node.get("name"): node.get("value") for node in parsed["resources/vars.xml"].findall("./variables/variable")}
    for name, expected in (("screen_w", PANEL_W), ("center_x", CENTER), ("content_w", CONTENT_W), ("nav_item_w", CONTENT_W // 4)):
        if values[name] != str(expected):
            raise ValueError("incorrect horizontal variable: " + name)
    for index in range(1, 5):
        if int(values[f"np_pill_x{index}"]) + 96 != int(values[f"nav_item_{index}"]):
            raise ValueError("navigation pill is not centered on its touch target")
    for tree in parsed.values():
        for placement in tree.iter("placement"):
            for axis in ("x", "y", "w", "h"):
                value = placement.get(axis, "")
                if value and not re.fullmatch(r"-?\d+|%[^%]+%|[A-Za-z_]\w*", value):
                    raise ValueError("placement arithmetic is not supported: " + str(placement.attrib))
            x, width = placement.get("x", ""), placement.get("w", "")
            if x.isdigit() and width.isdigit() and int(x) + int(width) > PANEL_W and placement.get("placement", "0") == "0":
                raise ValueError("horizontal control extends beyond the framebuffer: " + str(placement.attrib))
    for screen_height in (PANEL_H, PANEL_H + 96):
        cutout = 28
        if (330 + cutout) + (screen_height - cutout - 714) > screen_height - 360 - 24:
            raise ValueError("horizontal menu viewport overlaps navigation")
        if screen_height - 912 + KEYBOARD_H != screen_height - 144:
            raise ValueError("keyboard overlaps bottom navigation")
    return len(parsed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default="/root/fox_14.1")
    parser.add_argument("--check", action="store_true", help="validate existing generated resources without writing")
    parser.add_argument("--dry-run", action="store_true", help="preflight the full source conversion without writing")
    args = parser.parse_args()
    theme_root = Path(args.root) / "bootable/recovery/gui/theme"
    source, destination = theme_root / "portrait_hdpi", theme_root / "landscape_hdpi"
    if args.check:
        files = {path.relative_to(destination).as_posix(): path.read_bytes() for path in destination.rglob("*") if path.is_file()}
        print(f"PASS: {validate_landscape(files)} built-in horizontal XML resources")
        return
    files = build_landscape_files(source)
    if args.dry_run:
        print(f"PASS: source conversion; {len(files)} built-in horizontal resources; no files written")
        return
    existing = {path.relative_to(destination).as_posix(): path.read_bytes() for path in destination.rglob("*") if path.is_file()}
    if files == existing:
        print("Built-in horizontal resources already current: " + str(destination))
        return
    with tempfile.TemporaryDirectory(prefix=".landscape-stage-", dir=theme_root) as temporary:
        staged = Path(temporary) / "landscape_hdpi"
        for name, data in files.items():
            target = staged / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        backup = None
        if destination.exists():
            backup = Path(tempfile.mkdtemp(prefix="landscape_hdpi.previous-", dir=theme_root))
            backup.rmdir()
            destination.rename(backup)
        try:
            staged.rename(destination)
        except OSError:
            if backup is not None:
                backup.rename(destination)
            raise
        if backup is not None:
            print("Previous generated resources preserved at " + str(backup))
    print(f"Built-in horizontal resources: {destination}; {len(files)} files, {PANEL_W}x{PANEL_H}, scale=1x1")


if __name__ == "__main__":
    main()
