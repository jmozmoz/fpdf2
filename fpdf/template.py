"""
PDF template helpers for fpdf2.

Usage documentation at: <https://py-pdf.github.io/fpdf2/Templates.html>
"""

__author__ = "Mariano Reingart <reingart@gmail.com>"
__copyright__ = "Copyright (C) 2010 Mariano Reingart"
__license__ = "LGPL 3.0"

import csv
import json
import locale
import os
import warnings
from typing import Any, Optional, Sequence

from .deprecation import get_stack_level
from .enums import WrapMode, XPos, YPos
from .errors import FPDFException
from .fpdf import FPDF

def _rgb(col: int) -> tuple[float, float, float]:
    return (col // 65536), (col // 256 % 256), (col % 256)


def _rgb_as_str(col: int) -> str:
    r, g, b = _rgb(col)
    if (r == 0 and g == 0 and b == 0) or g == -1:
        return f"{r / 255:.3f} g"
    return f"{r / 255:.3f} {g / 255:.3f} {b / 255:.3f} rg"


class FlexTemplate:
    """
    A flexible templating class.

    Allows to apply one or several template definitions to any page of
    a document in any combination.

    Usage documentation at: <https://py-pdf.github.io/fpdf2/Templates.html>
    """

    def __init__(
        self, pdf: FPDF, elements: Optional[Sequence[dict[str, Any]]] = None
    ) -> None:
        """
        Args:
            pdf (fpdf.fpdf.FPDF): All content will be added to this `FPDF` instance.
            elements (list[dict]): A template definition in a list of dicts.
                If you omit this, then you need to call either load_elements()
                or parse_csv() before doing anything else.
        """
        if not isinstance(pdf, FPDF):
            raise TypeError("'pdf' must be an instance of fpdf.FPDF()")
        self.pdf = pdf
        self.splitting_pdf: Optional[FPDF] = None  # for split_multicell()
        if elements:
            self.load_elements(elements)
        self.handlers = {
            "T": self._text,
            "L": self._line,
            "I": self._image,
            "B": self._rect,
            "E": self._ellipse,
            "BC": self._barcode,
            "C39": self._code39,
            "W": self._write,
        }
        self.texts: dict[str, Any] = {}

    def load_elements(self, elements: Sequence[dict[str, Any]]) -> None:
        """
        Load a template definition.

        Args:
            elements (list of dicts): A template definition in a list of dicts
        """
        key_config = {
            # key: type
            "name": (str, type(None)),
            "type": (str, type(None)),
            "x1": (int, float, type(None)),
            "y1": (int, float, type(None)),
            "x2": (int, float, type(None)),
            "y2": (int, float, type(None)),
            "w": (int, float, type(None)),
            "h": (int, float, type(None)),
            "new_x": (str, XPos, type(None)),
            "new_y": (str, YPos, type(None)),
            "font": (str, type(None)),
            "size": (int, float),
            "bold": object,  # "bool or equivalent"
            "italic": object,
            "underline": object,
            "foreground": int,
            "background": int,
            "align": (str, type(None)),
            "text": (str, type(None)),
            "priority": int,
            "multiline": (bool, type(None)),
            "rotate": (int, float),
            "wrapmode": (str, type(None)),
            "dash_pattern": (dict, type(None)),
            "keep_aspect_ratio": (int, type(None)),
        }

        self.elements: Sequence[dict[str, Any]] = elements
        self.keys: list[str] = []
        for e in elements:
            # priority is optional, but we need a default for sorting.
            if not "priority" in e:
                e["priority"] = 0
            for k in ("name", "type"):
                if k not in e:
                    if e["type"] == "C39":
                        # lots of legacy special casing.
                        # We need to do that here, so that rotation and scaling
                        # still work.
                        if k == "x1" and "x" in e:
                            e["x1"] = e["x"]
                            continue
                        if k == "y1" and "y" in e:
                            e["y1"] = e["y"]
                            continue
                        if k == "y2" and "h" in e:
                            e["y2"] = e["y1"] + e["h"]
                            continue
                    raise KeyError(f"Mandatory key '{k}' missing in input data")

            # An element can either use the traditional absolute coordinates
            # or automatic positioning using the current layout cursor.
            #
            # x2/y2 remain optional for flow-layout elements because they can
            # be derived from x1/y1 + w/h.
            if "x2" not in e and e["type"] in ["BC", "C39"]:
                e["x2"] = 0

            has_flow_properties = any(
                key in e for key in ("w", "h", "new_x", "new_y")
            )
            has_coordinates = any(
                key in e for key in ("x1", "y1", "x2", "y2")
            )

            print('debug!')
            print(has_flow_properties, has_coordinates)
            if has_flow_properties and not has_coordinates:
                # Fully automatic element: x/y will be taken from the
                # layout cursor at render time.
                pass
            elif not has_coordinates:
                raise KeyError(
                    "Template element must define either x1/y1/x2/y2 "
                    "or flow-layout properties"
                )

            # If any coordinate is missing, it must be possible to derive it
            # from the corresponding flow dimension.
            if "x1" not in e and "w" not in e:
                raise KeyError(
                    "Missing 'x1': automatic positioning requires 'w'"
                )
            if "y1" not in e and "h" not in e:
                raise KeyError(
                    "Missing 'y1': automatic positioning requires 'h'"
                )
            if "x2" not in e and "w" not in e and e["type"] not in ["BC", "C39"]:
                raise KeyError(
                    "Missing 'x2': automatic positioning requires 'w'"
                )
            if "y2" not in e and "h" not in e:
                raise KeyError(
                    "Missing 'y2': automatic positioning requires 'h'"
                )

            # x2 is optional for barcode types, but needed for offset rendering
            if "x2" not in e:
                if e["type"] in ["BC", "C39"]:
                    e["x2"] = 0
            if not "size" in e and e["type"] == "C39":
                if "w" in e:
                    e["size"] = e["w"]
            if "dash_pattern" in e:
                for k in e["dash_pattern"].keys():
                    if k not in ("dash", "gap", "phase"):
                        raise KeyError(f"Invalid key '{k}' in dash_pattern")
                if "dash" not in e["dash_pattern"]:
                    e["dash_pattern"]["dash"] = 0
                if "gap" not in e["dash_pattern"]:
                    e["dash_pattern"]["gap"] = e["dash_pattern"]["dash"]
                if "phase" not in e["dash_pattern"]:
                    e["dash_pattern"]["phase"] = 0

            for k, t in key_config.items():
                if k in e and not isinstance(e[k], t):  # type: ignore[arg-type]
                    ttype = (
                        t.__name__
                        if isinstance(t, type)
                        else " or ".join([f"'{x.__name__}'" for x in t])  # type: ignore[attr-defined]
                    )
                    raise TypeError(
                        f"Value of element item '{k}' must be {ttype}, not '{type(e[k]).__name__}'."
                    )
            self.keys.append(e["name"].lower())

    @staticmethod
    def _parse_colorcode(s: str) -> int:
        """Allow hex and oct values for colors"""
        if s[:2] in ["0x", "0X"]:
            return int(s, 16)
        if s[0] == "0":
            return int(s, 8)
        return int(s)

    @staticmethod
    def _parse_multiline(s: str) -> Optional[bool]:
        i = int(s)
        if i > 0:
            return True
        if i < 0:
            return False
        return None

    def parse_json(self, infile: os.PathLike[str], encoding: str = "utf-8") -> None:
        """
        Load the template definition from a JSON file.
        The data must be structured as an array of objects, with names and values exactly
        equivalent to what would get supplied to load_elements(),

        Args:
            infile (str, path-like object): The filepath of the JSON file.
            encoding (str): The character encoding of the file. Default is UTF-8.
        """
        with open(infile, encoding=encoding) as f:
            data = json.load(f)
            for d in data:
                fgval = d.get("foreground")
                if fgval and isinstance(fgval, str):
                    if fgval.lower().startswith("#"):
                        d["foreground"] = int(fgval[1:], 16)
                    else:
                        raise ValueError(
                            "If foreground is a string, it must have the form '#rrggbb'."
                        )
                bgval = d.get("background")
                if bgval and isinstance(bgval, str):
                    if bgval.lower().startswith("#"):
                        d["background"] = int(bgval[1:], 16)
                    else:
                        raise ValueError(
                            "If background is a string, it must have the form '#rrggbb'."
                        )
            self.load_elements(data)

    def parse_csv(
        self,
        infile: os.PathLike[str],
        delimiter: str = ",",
        decimal_sep: str = ".",
        encoding: Optional[str] = None,
    ) -> None:
        """
        Load the template definition from a CSV file.

        Args:
            infile (str, path-like object): The filepath of the CSV file.
            delimiter (str): The character that separates the fields in the CSV file:
                Usually a comma, semicolon, or tab.
            decimal_sep (str): The decimal separator used in the file.
                Usually either a point or a comma.
            encoding (str): The character encoding of the file.
                Default is the system default encoding.
        """

        def _varsep_float(s: str, default: str = "0") -> float:
            """Convert to float with given decimal separator"""
            # glad to have nonlocal scoping...
            return float((s.strip() or default).replace(decimal_sep, "."))

        key_config = (
            # key, converter, mandatory
            ("name", str, True),
            ("type", str, True),
            ("x1", _varsep_float, True),
            ("y1", _varsep_float, True),
            ("x2", _varsep_float, True),
            ("y2", _varsep_float, True),
            ("font", str, False),
            ("size", _varsep_float, False),
            ("bold", int, False),
            ("italic", int, False),
            ("underline", int, False),
            ("foreground", self._parse_colorcode, False),
            ("background", self._parse_colorcode, False),
            ("align", str, False),
            ("text", str, False),
            ("priority", int, False),
            ("multiline", self._parse_multiline, False),
            ("rotate", _varsep_float, False),
            ("wrapmode", str, False),
            ("keep_aspect_ratio", int, False),
            ("w", _varsep_float, False),
            ("h", _varsep_float, False),
            ("new_x", str, False),
            ("new_y", str, False),
        )
        self.elements = []
        if encoding is None:
            encoding = locale.getpreferredencoding()
        with open(infile, encoding=encoding) as f:
            for row in csv.reader(f, delimiter=delimiter):
                # fill in blanks for any missing items
                row.extend([""] * (len(key_config) - len(row)))
                kargs: dict[str, Any] = {}
                for val, cfg in zip(row, key_config):
                    vs = val.strip()
                    if not vs:
                        if cfg[2]:  # mandatory
                            if cfg[0] == "x2" and row[1] in ["BC", "C39"]:
                                # two types don't need x2, but offset rendering does
                                pass
                            else:
                                raise FPDFException(
                                    f"Mandatory value '{cfg[0]}' missing in csv data"
                                )
                        elif cfg[0] == "priority":
                            # formally optional, but we need some value for sorting
                            kargs["priority"] = 0
                        # otherwise, let the type handlers use their own defaults
                    else:
                        kargs[cfg[0]] = cfg[1](vs)  # type: ignore[operator]
                self.elements.append(kargs)
        self.keys = [val["name"].lower() for val in self.elements]

    def __setitem__(self, name: str, value: Any) -> None:
        assert isinstance(
            name, str
        ), f"name must be of type 'str', not '{type(name).__name__}'."
        # value has too many valid types to reasonably check here
        if name.lower() not in self.keys:
            raise FPDFException(f"Element not loaded, cannot set item: {name}")
        self.texts[name.lower()] = value

    # setitem shortcut (may be further extended)
    set = __setitem__

    def __contains__(self, name: str) -> bool:
        assert isinstance(
            name, str
        ), f"name must be of type 'str', not '{type(name).__name__}'."
        return name.lower() in self.keys

    def __getitem__(self, name: str) -> Any:
        assert isinstance(
            name, str
        ), f"name must be of type 'str', not '{type(name).__name__}'."
        if name not in self.keys:
            raise KeyError(name)
        key = name.lower()
        if key in self.texts:
            # text for this page:
            return self.texts[key]
        # find first element for default text:
        return next(
            (x["text"] for x in self.elements if x["name"].lower() == key), None
        )

    def split_multicell(self, text: str, element_name: str) -> list[str]:
        """
        Split a string between words, for the parts to fit into a given element
        width. Additional splits will be made replacing any '\\n' characters.

        Args:
            text (str): The input text string.
            element_name (str): The name of the template element to fit the text inside.

        Returns:
            A list of substrings, each of which will fit into the element width
            when rendered in the element font style and size.
        """
        element = next(
            element
            for element in self.elements
            if element["name"].lower() == element_name.lower()
        )
        if not self.splitting_pdf:
            self.splitting_pdf = FPDF()
            self.splitting_pdf.add_page()
        assert self.splitting_pdf is not None
        style = ""
        if element.get("bold"):
            style += "B"
        if element.get("italic"):
            style += "I"
        if element.get("underline"):
            style += "U"
        self.splitting_pdf.set_font(element["font"], style, element["size"])
        return self.splitting_pdf.multi_cell(  # type: ignore[return-value]
            w=element["x2"] - element["x1"],
            h=element["y2"] - element["y1"],
            text=str(text),
            align=element.get("align", ""),
            dry_run=True,
            output="LINES",
            wrapmode=element.get("wrapmode", "WORD"),
        )

    def _text(
        self,
        *_: Any,
        x1: float = 0,
        y1: float = 0,
        x2: float = 0,
        y2: float = 0,
        text: str = "",
        font: str = "helvetica",
        size: float = 10,
        scale: float = 1.0,
        bold: bool = False,
        italic: bool = False,
        underline: bool = False,
        align: str = "",
        foreground: int = 0,
        background: Optional[int] = None,
        multiline: Optional[bool] = None,
        wrapmode: str = "WORD",
        **__: Any,
    ) -> None:
        if not text:
            return
        pdf = self.pdf
        if pdf.text_color != _rgb_as_str(foreground):  # type: ignore[comparison-overlap]
            pdf.set_text_color(*_rgb(foreground))
        if background is None:
            fill = False
        else:
            fill = True
            if pdf.fill_color != _rgb_as_str(background):  # type: ignore[comparison-overlap]
                pdf.set_fill_color(*_rgb(background))

        font = font.strip().lower()
        style = ""
        for tag in "B", "I", "U":
            if text.startswith(f"<{tag}>") and text.endswith(f"</{tag}>"):
                text = text[3:-4]
                style += tag
        if bold:
            style += "B"
        if italic:
            style += "I"
        if underline:
            style += "U"
        pdf.set_font(font, style, size * scale)
        print('debug8')
        pdf.set_xy(x1, y1)
        width, height = x2 - x1, y2 - y1
        if multiline is None:  # write without wrapping/trimming (default)
            pdf.cell(w=width, h=height, text=text, border=0, align=align, fill=fill)
        elif multiline:  # automatic word - warp
            print('multi_cell:', width, height, text, align, fill)
            pdf.multi_cell(
                w=width,
                h=height,
                text=text,
                border=0,
                align=align,
                fill=fill,
                wrapmode=WrapMode.coerce(wrapmode),
            )
        else:  # trim to fit exactly the space defined
            text = pdf.multi_cell(  # type: ignore[index,assignment]
                w=width,
                h=height,
                text=text,
                align=align,
                wrapmode=WrapMode.coerce(wrapmode),
                dry_run=True,
                output="LINES",
            )[0]
            pdf.cell(w=width, h=height, text=text, border=0, align=align, fill=fill)

    def _line(
        self,
        *_: Any,
        x1: float = 0,
        y1: float = 0,
        x2: float = 0,
        y2: float = 0,
        size: float = 0,
        scale: float = 1.0,
        foreground: int = 0,
        dash_pattern: Optional[tuple[float, float, float]] = None,
        **__: Any,
    ) -> None:
        assert self.pdf.draw_color is not None
        if self.pdf.draw_color.serialize().lower() != _rgb_as_str(foreground):
            self.pdf.set_draw_color(*_rgb(foreground))
        self.pdf.set_line_width(size * scale)
        if dash_pattern is not None:
            # Save dash_pattern to restore after this line
            restore_dash_pattern = self.pdf.dash_pattern
            if isinstance(dash_pattern, dict):
                self.pdf.set_dash_pattern(
                    dash_pattern.get(  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                        "dash", 0
                    ),
                    dash_pattern.get(  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                        "gap", 0
                    ),
                    dash_pattern.get(  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                        "phase", 0
                    ),
                )
            else:
                self.pdf.set_dash_pattern(
                    dash_pattern[0], dash_pattern[1], dash_pattern[2]
                )
        self.pdf.line(x1, y1, x2, y2)
        if dash_pattern is not None:
            self.pdf.set_dash_pattern(
                **restore_dash_pattern  # pyright: ignore[reportPossiblyUnboundVariable]
            )

    def _rect(
        self,
        *_: Any,
        x1: float = 0,
        y1: float = 0,
        x2: float = 0,
        y2: float = 0,
        size: float = 0,
        scale: float = 1.0,
        foreground: int = 0,
        background: Optional[int] = None,
        dash_pattern: Optional[tuple[float, float, float]] = None,
        **__: Any,
    ) -> None:
        pdf = self.pdf
        assert pdf.draw_color is not None
        if pdf.draw_color.serialize().lower() != _rgb_as_str(foreground):
            pdf.set_draw_color(*_rgb(foreground))
        if background is None:
            style = "D"
        else:
            style = "FD"
            if pdf.fill_color != _rgb_as_str(background):  # type: ignore[comparison-overlap]
                pdf.set_fill_color(*_rgb(background))
        pdf.set_line_width(size * scale)
        if dash_pattern is not None:
            # Save dash_pattern to restore after this rect
            restore_dash_pattern = self.pdf.dash_pattern
            if isinstance(dash_pattern, dict):
                pdf.set_dash_pattern(
                    dash_pattern.get(  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                        "dash", 0
                    ),
                    dash_pattern.get(  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                        "gap", 0
                    ),
                    dash_pattern.get(  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                        "phase", 0
                    ),
                )
            else:
                pdf.set_dash_pattern(dash_pattern[0], dash_pattern[1], dash_pattern[2])
        pdf.rect(x1, y1, x2 - x1, y2 - y1, style=style)
        if dash_pattern is not None:
            self.pdf.set_dash_pattern(
                **restore_dash_pattern  # pyright: ignore[reportPossiblyUnboundVariable]
            )

    def _ellipse(
        self,
        *_: Any,
        x1: float = 0,
        y1: float = 0,
        x2: float = 0,
        y2: float = 0,
        size: float = 0,
        scale: float = 1.0,
        foreground: int = 0,
        background: Optional[int] = None,
        dash_pattern: Optional[tuple[float, float, float]] = None,
        **__: Any,
    ) -> None:
        pdf = self.pdf
        assert pdf.draw_color is not None
        if pdf.draw_color.serialize().lower() != _rgb_as_str(foreground):
            pdf.set_draw_color(*_rgb(foreground))
        if background is None:
            style = "D"
        else:
            style = "FD"
            if pdf.fill_color != _rgb_as_str(background):  # type: ignore[comparison-overlap]
                pdf.set_fill_color(*_rgb(background))
        pdf.set_line_width(size * scale)
        if dash_pattern is not None:
            # Save dash_pattern to restore after this ellipse
            restore_dash_pattern = self.pdf.dash_pattern
            if isinstance(dash_pattern, dict):
                pdf.set_dash_pattern(
                    dash_pattern.get(  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                        "dash", 0
                    ),
                    dash_pattern.get(  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                        "gap", 0
                    ),
                    dash_pattern.get(  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                        "phase", 0
                    ),
                )
            else:
                pdf.set_dash_pattern(dash_pattern[0], dash_pattern[1], dash_pattern[2])
        pdf.ellipse(x1, y1, x2 - x1, y2 - y1, style=style)
        if dash_pattern is not None:
            self.pdf.set_dash_pattern(
                **restore_dash_pattern  # pyright: ignore[reportPossiblyUnboundVariable]
            )

    def _image(
        self,
        *_: Any,
        x1: float = 0,
        y1: float = 0,
        x2: float = 0,
        y2: float = 0,
        text: str = "",
        keep_aspect_ratio: bool = False,
        **__: Any,
    ) -> None:
        if text:
            self.pdf.image(
                text,
                x1,
                y1,
                w=x2 - x1,
                h=y2 - y1,
                link="",
                keep_aspect_ratio=keep_aspect_ratio,
            )

    def _barcode(
        self,
        *_: Any,
        x1: float = 0,
        y1: float = 0,
        x2: float = 0,
        y2: float = 0,
        text: str = "",
        font: str = "interleaved 2of5 nt",
        size: float = 1,
        scale: float = 1.0,
        foreground: int = 0,
        **__: Any,
    ) -> None:
        # pylint: disable=unused-argument
        pdf = self.pdf
        assert pdf.fill_color is not None
        if pdf.fill_color.serialize().lower() != _rgb_as_str(foreground):
            pdf.set_fill_color(*_rgb(foreground))
        font = font.lower().strip()
        if font == "interleaved 2of5 nt":
            pdf.interleaved2of5(text, x1, y1, w=size * scale, h=y2 - y1)

    def _code39(
        self,
        *_: Any,
        x1: float = 0,
        y1: float = 0,
        y2: float = 0,
        text: str = "",
        size: float = 1.5,
        scale: float = 1.0,
        foreground: int = 0,
        x: Optional[float] = None,
        y: Optional[float] = None,
        w: Optional[float] = None,
        h: Optional[float] = None,
        **__: Any,
    ) -> None:
        if x is not None or y is not None or w is not None or h is not None:
            warnings.warn(
                (
                    "code39 arguments x/y/w/h are deprecated since v2.4.4,"
                    " please use x1/y1/y2/size instead"
                ),
                DeprecationWarning,
                stacklevel=get_stack_level(),
            )
        pdf = self.pdf
        assert pdf.fill_color is not None
        if pdf.fill_color.serialize().lower() != _rgb_as_str(foreground):
            pdf.set_fill_color(*_rgb(foreground))
        h = y2 - y1
        if h <= 0:
            h = 5
        pdf.code39(text, x1, y1, size * scale, h)

    # Added by Derek Schwalenberg Schwalenberg1013@gmail.com to allow (url) links in
    # templates (using write method) 2014-02-22
    def _write(
        self,
        *_: Any,
        x1: float = 0,
        y1: float = 0,
        x2: float = 0,
        y2: float = 0,
        text: str = "",
        font: str = "helvetica",
        size: float = 10,
        scale: float = 1.0,
        bold: bool = False,
        italic: bool = False,
        underline: bool = False,
        link: Optional[int | str] = "",
        foreground: int = 0,
        **__: Any,
    ) -> None:
        # pylint: disable=unused-argument
        if not text:
            return
        pdf = self.pdf
        if pdf.text_color != _rgb_as_str(foreground):  # type: ignore[comparison-overlap]
            pdf.set_text_color(*_rgb(foreground))
        font = font.strip().lower()
        style = ""
        for tag in "B", "I", "U":
            if text.startswith(f"<{tag}>") and text.endswith(f"</{tag}>"):
                text = text[3:-4]
                style += tag
        if bold:
            style += "B"
        if italic:
            style += "I"
        if underline:
            style += "U"
        pdf.set_font(font, style, size * scale)
        print('debug9')
        pdf.set_xy(x1, y1)
        pdf.write(5, text, link)

    def _resolve_element_position(
        self,
        element: dict[str, Any],
        cursor_x: float,
        cursor_y: float,
    ) -> tuple[float, float, float, float]:
        """
        Resolve an element's bounding box.

        Existing template elements use x1/y1/x2/y2.
        Flow-layout elements may omit coordinates and instead use the
        current layout cursor together with w/h.
        """
        x1 = element.get("x1")
        y1 = element.get("y1")
        x2 = element.get("x2")
        y2 = element.get("y2")
        w = element.get("w")
        h = element.get("h")

        if x1 is None:
            x1 = cursor_x
        if y1 is None:
            y1 = cursor_y

        if x2 is None:
            if w is None:
                if element["type"].upper() in ("T", "W"):
                    # Match FPDF.cell(): omitted width means text width.
                    style = ""
                    if element.get("bold"):
                        style += "B"
                    if element.get("italic"):
                        style += "I"
                    if element.get("underline"):
                        style += "U"

                    with self.pdf.local_context():
                        self.pdf.set_font(
                            element.get("font") or "helvetica",
                            style=style,
                            size=element.get("size", 10),
                        )
                        w = self.pdf.get_string_width(
                            str(element.get("text") or "")
                        )
                else:
                    raise KeyError(
                        f"Element '{element.get('name')}' requires "
                        "either 'x2' or 'w'"
                    )

            # Match FPDF.cell(w=0): extend to the right margin.
            if w == 0:
                x2 = self.pdf.w - self.pdf.r_margin
            else:
                x2 = x1 + w

        if y2 is None:
            if h is None:
                if element["type"].upper() in ("T", "W"):
                    # Match FPDF.cell(): omitted height means current
                    # font size.
                    h = element.get("size", 10)
                else:
                    raise KeyError(
                        f"Element '{element.get('name')}' requires "
                        "either 'y2' or 'h'"
                    )
            y2 = y1 + h

        return float(x1), float(y1), float(x2), float(y2)

    @staticmethod
    def _update_layout_cursor(
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        new_x: XPos | str | None,
        new_y: YPos | str | None,
        pdf: FPDF,
    ) -> tuple[float, float]:
        """
        Calculate the next layout cursor using the same semantics as
        FPDF.cell(new_x=..., new_y=...).
        """
        new_x = XPos.coerce(new_x or XPos.RIGHT)
        new_y = YPos.coerce(new_y or YPos.TOP)

        if new_x == XPos.LEFT:
            cursor_x = x1
        elif new_x == XPos.RIGHT:
            cursor_x = x2
        elif new_x == XPos.LMARGIN:
            cursor_x = pdf.l_margin
        elif new_x == XPos.RMARGIN:
            cursor_x = pdf.w - pdf.r_margin
        elif new_x in (XPos.START, XPos.END, XPos.WCONT, XPos.CENTER):
            # These values depend on the actual rendered text width in
            # FPDF.cell(). A template element has a bounding box, so the
            # corresponding positions are derived from that box.
            if new_x == XPos.START:
                cursor_x = x1
            elif new_x == XPos.END:
                cursor_x = x2
            elif new_x == XPos.WCONT:
                cursor_x = x2
            else:
                cursor_x = x1 + (x2 - x1) / 2
        else:
            cursor_x = x1

        if new_y in (YPos.TOP, YPos.LAST):
            cursor_y = y1
        elif new_y == YPos.NEXT:
            cursor_y = y2
        elif new_y == YPos.TMARGIN:
            cursor_y = pdf.t_margin
        elif new_y == YPos.BMARGIN:
            cursor_y = pdf.h - pdf.b_margin
        else:
            cursor_y = y1

        return cursor_x, cursor_y

    def render(
        self,
        offsetx: float = 0.0,
        offsety: float = 0.0,
        rotate: float = 0.0,
        scale: float = 1.0,
    ) -> None:
        """
        Add the contents of the template to the PDF document.

        Args:
            offsetx, offsety (float): Place the template to move its origin to the given coordinates.
            rotate (float): Rotate the inserted template around its (offset) origin.
            scale (float): Scale the inserted template by this factor.
        """
        sorted_elements = sorted(self.elements, key=lambda x: x["priority"])

        with self.pdf.local_context():
            for element in sorted_elements:
                ele = element.copy()  # don't want to modify the callers original
                ele["text"] = self.texts.get(ele["name"].lower(), ele.get("text", ""))

                print("render start:", self.pdf.x, self.pdf.y)

                # Resolve automatic coordinates before applying the existing
                # scale/offset transformation.
                (
                    ele["x1"],
                    ele["y1"],
                    ele["x2"],
                    ele["y2"],
                ) = self._resolve_element_position(
                    ele,
                    self.pdf.x,
                    self.pdf.y,
                )

                if scale != 1.0:
                    ele["x1"] = ele["x1"] * scale
                    ele["y1"] = ele["y1"] * scale
                    ele["x2"] = ele["x1"] + ((ele["x2"] - element["x1"]) * scale)
                    ele["y2"] = ele["y1"] + ((ele["y2"] - element["y1"]) * scale)
                if offsetx:
                    ele["x1"] = ele["x1"] + offsetx
                    ele["x2"] = ele["x2"] + offsetx
                if offsety:
                    ele["y1"] = ele["y1"] + offsety
                    ele["y2"] = ele["y2"] + offsety
                ele["scale"] = scale
                handler_name = ele["type"].upper()
                if rotate:  # don't rotate by 0.0 degrees
                    with self.pdf.rotation(rotate, offsetx, offsety):
                        if "rotate" in ele and ele["rotate"]:
                            with self.pdf.rotation(ele["rotate"], ele["x1"], ele["y1"]):
                                self.handlers[handler_name](**ele)
                        else:
                            self.handlers[handler_name](**ele)
                else:
                    if "rotate" in ele and ele["rotate"]:
                        with self.pdf.rotation(ele["rotate"], ele["x1"], ele["y1"]):
                            self.handlers[handler_name](**ele)
                    else:
                        self.handlers[handler_name](**ele)

                # Only flow-layout elements move the cursor. Existing
                # absolute-position templates therefore retain their exact
                # behaviour.
                if any(
                    key in element
                    for key in ("w", "h", "new_x", "new_y")
                ):
                    print("render before update cursor pdf.x,.y:", self.pdf.x, self.pdf.y)
                    # cursor_x, cursor_y = self._update_layout_cursor(
                    #     ele["x1"],
                    #     ele["y1"],
                    #     ele["x2"],
                    #     ele["y2"],
                    #     element.get("new_x"),
                    #     element.get("new_y"),
                    #     self.pdf,
                    # )
                    # print("render stop:", cursor_x, cursor_y)
                    # self.pdf.set_xy(cursor_x, cursor_y)

        self.texts = {}  # reset modified entries for the next page


class Template(FlexTemplate):
    """
    A simple templating class.

    Allows to apply a single template definition to all pages of a document.

    Usage documentation at: <https://py-pdf.github.io/fpdf2/Templates.html>
    """

    # Disabling this check due to the "format" parameter below:
    # pylint: disable=redefined-builtin
    def __init__(
        self,
        infile: Optional[os.PathLike[str]] = None,
        elements: Optional[Sequence[dict[str, Any]]] = None,
        format: str = "A4",
        orientation: str = "portrait",
        unit: str = "mm",
        title: str = "",
        author: str = "",
        subject: str = "",
        creator: str = "",
        keywords: str = "",
    ) -> None:
        """
        Args:
            infile (str): [**DEPRECATED since 2.2.0**] unused, will be removed in a later version
            elements (list[dict]): A template definition in a list of dicts.
                If you omit this, then you need to call either load_elements()
                or parse_csv() before doing anything else.
            format (str): The page format of the document (eg. "A4" or "letter").
            orientation (str): The orientation of the document.
                Possible values are "portrait"/"P" or "landscape"/"L"
            unit (str): The units used in the template definition.
                One of "mm", "cm", "in", "pt", or a number for points per unit.
            title (str): The title of the document.
            author (str): The author of the document.
            subject (str): The subject matter of the document.
            creator (str): The creator of the document.
        """
        if infile:
            warnings.warn(
                '"infile" is deprecated since v2.2.0, unused and will soon be removed',
                DeprecationWarning,
                stacklevel=get_stack_level(),
            )
        for arg in (
            "format",
            "orientation",
            "unit",
            "title",
            "author",
            "subject",
            "creator",
            "keywords",
        ):
            # nosemgrep: python.lang.security.dangerous-globals-use.dangerous-globals-use
            if not isinstance(locals()[arg], str):
                raise TypeError(f'Argument "{arg}" must be of type str.')
        pdf = FPDF(format=format, orientation=orientation, unit=unit)
        pdf.set_title(title)
        pdf.set_author(author)
        pdf.set_creator(creator)
        pdf.set_subject(subject)
        pdf.set_keywords(keywords)
        super().__init__(pdf=pdf, elements=elements)

    def add_page(self) -> None:
        """Finish the current page, and proceed to the next one."""
        if self.pdf.page:
            self.render()
        self.pdf.add_page()

    # pylint: disable=arguments-differ
    def render(self, outfile: Optional[str] = None, dest: Optional[str] = None) -> None:  # type: ignore[override]
        """
        Finish the document and process all pending data.

        Args:
            outfile (str): If given, the PDF file will be written to this file name.
                Alternatively, the `.pdf.output()` method can be manually called.
            dest (str): [**DEPRECATED since 2.2.0**] unused, will be removed in a later version.
        """
        if dest:
            warnings.warn(
                '"dest" is deprecated since v2.2.0, unused and will soon be removed',
                DeprecationWarning,
                stacklevel=get_stack_level(),
            )
        self.pdf.set_font("helvetica", style="B", size=16)
        self.pdf.set_auto_page_break(False, margin=0)
        super().render()
        if outfile:
            self.pdf.output(outfile)
