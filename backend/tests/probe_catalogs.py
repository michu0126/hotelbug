"""Read official robots files to discover published hotel catalog sitemaps."""

import argparse
import asyncio

import httpx
from lxml import etree

ROOTS = {
    "marriott": "https://www.marriott.com",
    "ihg": "https://www.ihg.com",
    "gha": "https://www.ghadiscovery.com",
    "accor": "https://all.accor.com",
    "hilton": "https://www.hilton.com",
    "hyatt": "https://www.hyatt.com",
}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy")
    parser.add_argument("--sitemaps", action="store_true")
    parser.add_argument("--url", action="append")
    args = parser.parse_args()
    async with httpx.AsyncClient(proxy=args.proxy, timeout=20, follow_redirects=True) as client:
        targets = {key: value + "/robots.txt" for key, value in ROOTS.items()}
        if args.sitemaps:
            targets = {
                "ihg": "https://www.ihg.com/services/sitemaps/sitemap-index.xml",
                "gha": "https://www.ghadiscovery.com/sitemap.xml",
                "accor": "https://all.accor.com/sitemap-fh.xml",
                "hilton": "https://www.hilton.com/sitemap.xml",
            }
        if args.url:
            targets = {f"url-{index}": url for index, url in enumerate(args.url)}
        for provider, url in targets.items():
            try:
                response = await client.get(url)
                print(provider, response.status_code, str(response.url), flush=True)
                if response.is_success:
                    if "<urlset" in response.text or "<sitemapindex" in response.text:
                        tree = etree.fromstring(
                            response.content, etree.XMLParser(resolve_entities=False, no_network=True)
                        )
                        links = tree.xpath('//*[local-name()="loc"]/text()')
                        print(" ", etree.QName(tree).localname, "links=", len(links), flush=True)
                        for link in links[:12]:
                            print(" ", link, flush=True)
                    for line in response.text.splitlines():
                        if line.lower().startswith("sitemap:"):
                            print(" ", line, flush=True)
            except httpx.HTTPError as exc:
                print(provider, type(exc).__name__, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
