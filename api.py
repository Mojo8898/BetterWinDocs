from __future__ import annotations

import sys
from typing import Optional

import requests
from bs4 import BeautifulSoup

SEARCH_BASE = "https://learn.microsoft.com/api/search"
DOCS_HOST = "https://learn.microsoft.com"


class MSFTLearnScrapper:
    """
    Given a Win32 function name, tries to locate the corresponding Win32 API docs page
    on learn.microsoft.com and extract:
      - og:description (short summary)
      - Syntax section (code/pre block)
      - Parameters paragraphs under the Parameters h2
      - Return value paragraphs under the Return value h2

    NOTE: The search strategy (Microsoft search API -> filter win32 api pages -> title prefix match)
    is preserved from your original approach.
    """

    def __init__(self, function: str):
        self.requested_name = function
        self.resolved_name: Optional[str] = None
        self.soup: Optional[BeautifulSoup] = None
        self.h2_titles: dict[str, list[str]] = {}

        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": "BinaryNinja-WinDocSidebar/1.0",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

        # First try the exact name (e.g. OpenMutexA)
        self._init_from_name(function)
        if self.soup is not None:
            self.resolved_name = function
            return

        # If that failed and name ends with A/W, try the base
        if function and function[-1] in ("A", "W"):
            base = function[:-1]
            self._init_from_name(base)
            if self.soup is not None:
                # For an ANSI request (XxxA), docs page is really the Unicode form (XxxW)
                if function.endswith("A"):
                    self.resolved_name = base + "W"
                else:
                    self.resolved_name = function
                return

    def _init_from_name(self, name: str) -> None:
        params = {
            "search": name,
            "locale": "en-us",
            "$filter": "(category eq 'Documentation')",
        }

        resp = self._session.get(SEARCH_BASE, params=params, timeout=10)
        resp.raise_for_status()
        json_data = resp.json()

        results = json_data.get("results") or []
        if not results:
            return

        url = None
        for res in results:
            title = (res.get("title") or "").strip()
            candidate_url = (res.get("url") or "").strip()

            if not candidate_url:
                continue

            # Normalize relative URLs just in case
            if candidate_url.startswith("/"):
                candidate_url = DOCS_HOST + candidate_url

            # Keep the Win32 API docs scope filter you already had
            if "/windows/win32/api/" not in candidate_url:
                continue

            # Keep your "title starts with name" heuristic
            if not title.startswith(name):
                continue

            # Keep your "avoid matching partial words" heuristic
            end = len(name)
            if end < len(title) and title[end].islower():
                continue

            url = candidate_url
            break

        if not url:
            return

        content = self._session.get(url, timeout=10).content
        soup = BeautifulSoup(content, features="html.parser")
        self.soup = soup

        self.h2_titles.clear()
        for p in soup.select("p"):
            h2 = p.find_previous("h2")
            if not h2:
                continue
            key = h2.get_text(strip=True)
            txt = p.get_text(" ", strip=True)
            if not txt:
                continue
            self.h2_titles.setdefault(key, []).append(txt)

    def found_function_docs(self) -> bool:
        """
        Treat it as "function docs" only if a Syntax section exists.
        This is important because many non-function pages (types/structs) won't have Syntax.
        """
        return self.get_syntax() is not None

    def get_description(self, check: bool = False) -> str:
        if self.soup is None:
            return f"No Win32 docs found for {self.requested_name}"
        if check and self.get_syntax() is None:
            return ""

        meta = self.soup.find("meta", property="og:description")
        base_desc = meta["content"] if meta and meta.get("content") else ""

        note = ""
        if self.resolved_name and self.resolved_name != self.requested_name:
            note = (
                f"[Docs resolved to: {self.resolved_name} "
                f"(requested: {self.requested_name})]\n\n"
            )

        return note + base_desc

    def get_syntax(self) -> Optional[str]:
        if self.soup is None:
            return None

        for h2 in self.soup.find_all("h2"):
            if h2.get_text(strip=True) == "Syntax":
                # Prefer a <pre> (code block) if present
                pre = h2.find_next("pre")
                if pre is not None:
                    return pre.get_text("\n", strip=True)

                # Fallback: first element after h2
                nxt = h2.find_next()
                return nxt.get_text("\n", strip=True) if nxt else None

        return None

    def get_parameters(self) -> Optional[list[str]]:
        return self.h2_titles.get("Parameters")

    def get_return_value(self) -> Optional[list[str]]:
        return self.h2_titles.get("Return value")


if __name__ == "__main__":
    function_name = sys.argv[1] if len(sys.argv) > 1 else "GetProcAddress"

    scr = MSFTLearnScrapper(function_name)
    if scr.soup is None:
        print(f"No Win32 docs found for {function_name}")
        sys.exit(0)

    print("==== description ====")
    print(scr.get_description(check=True))
    print("==== syntax ====")
    print(scr.get_syntax())
    print("==== params ====")
    print(scr.get_parameters())
    print("==== return value ====")
    print(scr.get_return_value())
