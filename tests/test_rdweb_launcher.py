# -*- coding: utf-8 -*-
"""Юнит-тесты не-GUI логики RD Web Launcher (запускаются на любой ОС)."""
import configparser
import unittest

import rdweb_launcher as L


ATOM_SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
 <entry>
  <title>Корпоративный портал</title>
  <id>https://rdweb.contoso.com/RDWeb/Feed/jdoe/RemoteApplications%2fPortal</id>
  <d:SpecialFolderName>RDWEBFOLDER</d:SpecialFolderName>
  <link rel="edit" href="/RDWeb/Pages/en-US/pages/Downloads.aspx?Alias=RemoteApplications%2fPortal&amp;Name=%d0%9f%d0%be%d1%80%d1%82%d0%b0%d0%bb&amp;Type=Application"/>
 </entry>
 <entry>
  <title>My Desktop</title>
  <content type="application/xml">
    <m:properties><d:Name>WS01</d:Name></m:properties>
  </content>
  <link rel="edit" href="/RDWeb/Pages/en-US/pages/Desktops.aspx?Alias=RemoteDesktop%2fWS01&amp;Name=WS01&amp;Type=Desktop"/>
 </entry>
</feed>"""

HTML_SAMPLE = """<html><body>
<a href="UserGroup_RDPConn.asp?Alias=RemoteApplications%2fNotepad&amp;Name=Notepad&amp;Type=Application&amp;FilePath=x">Notepad</a>
<a href="Default.aspx?Alias=RemoteDesktop%2fSRV01&amp;Name=SRV01&amp;Type=Desktop">Desktop SRV01</a>
<a href="#top">skip</a>
</body></html>"""


def cfg():
    c = configparser.ConfigParser(interpolation=None, delimiters=("=",))
    c.optionxform = str  # как в load_config() — сохраняем ключи как есть
    c.read_string(L.DEFAULT_CONFIG_TEXT)
    return c


class TestParsing(unittest.TestCase):
    def test_atom_feed(self):
        items = L.parse_atom_feed(ATOM_SAMPLE)
        self.assertEqual(len(items), 2)
        by_alias = {i.alias: i for i in items}
        self.assertIn("RemoteApplications/Portal", by_alias)
        self.assertIn("RemoteDesktop/WS01", by_alias)
        self.assertEqual(by_alias["RemoteDesktop/WS01"].type, "Desktop")
        folders = {i.folder for i in items}
        self.assertIn("RemoteApplications", folders)
        self.assertIn("RemoteDesktop", folders)

    def test_html_page(self):
        items = L.parse_web_page(
            HTML_SAMPLE, "https://rdweb.contoso.com/RDWeb/Pages/en-US/Default.aspx")
        self.assertEqual(len(items), 2)
        by_alias = {i.alias: i for i in items}
        self.assertIn("RemoteApplications/Notepad", by_alias)
        self.assertEqual(by_alias["RemoteDesktop/SRV01"].type, "Desktop")

    def test_tree(self):
        items = L.parse_web_page(
            HTML_SAMPLE, "https://h/RDWeb/Pages/en-US/Default.aspx")
        tree = L.build_tree(items)
        self.assertEqual(sorted(tree), ["RemoteApplications", "RemoteDesktop"])


class TestRdpGeneration(unittest.TestCase):
    def test_generated_rdp_by_rules(self):
        res = L.Resource("RemoteDesktop/SRV01", "SRV01", "", "Desktop")
        content, how = L.acquire_rdp(None, cfg(), "https://h/RDWeb/", True, res)
        self.assertEqual(how, "generated")
        self.assertIn("full address:s:SRV01", content)
        self.assertIn("screenmodeid:i=1", content)

    def test_downloaded_rdp_merged_not_overwritten(self):
        res = L.Resource("RemoteApplications/App", "App", "app.rdp")

        class FakeResp:
            content = "full address:s:other-host\r\nscreenmodeid:i:0\r\n".encode()

            def raise_for_status(self):
                pass

        class FakeSession:
            def get(self, url, **kw):
                return FakeResp()

        content, how = L.acquire_rdp(
            FakeSession(), cfg(), "https://h/RDWeb/", True, res)
        self.assertEqual(how, "downloaded")
        compact = content.replace(" ", "")
        # скачанные значения имеют приоритет над правилами
        self.assertIn("full address:s:other-host", content)
        self.assertIn("screenmodeid:i:0", compact)
        self.assertNotIn("screenmodeid:i=1", compact)
        # недостающие правила добавлены
        self.assertIn("audiomode:i=0", compact)


class TestConfig(unittest.TestCase):
    def test_default_config_parses(self):
        c = cfg()
        self.assertEqual(c.get("launcher", "mstsc"),
                         r"C:\Windows\System32\mstsc.exe")
        self.assertEqual(c.get("launcher", "rdp_dir"), "")

    def test_current_user_no_password(self):
        # Функция должна работать без запроса пароля
        self.assertIsInstance(L.current_user(), str)


if __name__ == "__main__":
    unittest.main()
