"""Import a checksum-verified binary pair from an existing public GitHub release."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request


def require(condition, message):
    if not condition:
        raise ValueError(message)


def repository(value):
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", value), "Invalid repository")
    return value


def version_info(tag):
    match = re.fullmatch(r"(android|windows)-v(\d+\.\d+\.\d+)(?:-(\d+))?", tag)
    require(match is not None, "Unsupported release tag")
    platform, version, build = match.groups()
    require((platform == "android") == bool(build), "Invalid platform tag")
    return platform, version, build


def digest(asset):
    value = asset.get("digest", "")
    require(re.fullmatch(r"sha256:[a-f0-9]{64}", value), "Missing SHA-256 asset digest")
    return value[7:]


def validate_source(source, tag, expected, repo, release):
    repository(source)
    platform, version, build = version_info(tag)
    require(re.fullmatch(r"[a-f0-9]{64}", expected), "Invalid expected SHA-256")
    require(repo.get("private") is False and repo.get("visibility") == "public", "Source must be public")
    require(repo.get("full_name", "").lower() == source.lower(), "Source repository mismatch")
    require(release.get("tag_name") == tag and release.get("draft") is False and release.get("published_at"), "Source must be published")
    require(isinstance(release.get("prerelease"), bool), "Missing release status")
    assets = release.get("assets", [])
    require(len(assets) == 2, "Expected exactly one package and its checksum")
    package = next((asset for asset in assets if not asset["name"].endswith(".sha256")), None)
    require(package is not None, "Missing package")
    pattern = (rf"Doubo-Android-{re.escape(version)}-{build}\.apk" if platform == "android"
               else rf"Doubo-Windows-x64-{re.escape(version)}(?:-r\d+)?\.zip")
    require(re.fullmatch(pattern, package["name"]), "Package name does not match release")
    checksum = next((asset for asset in assets if asset["name"] == package["name"] + ".sha256"), None)
    require(checksum is not None, "Missing matching checksum")
    for asset, limit in ((package, 512 * 1024 * 1024), (checksum, 4096)):
        require(isinstance(asset.get("size"), int) and 0 < asset["size"] <= limit, "Invalid asset size")
        digest(asset)
        expected_url = f"https://github.com/{source}/releases/download/{tag}/{asset['name']}"
        require(asset.get("browser_download_url") == expected_url, "Unexpected public download URL")
    require(digest(package) == expected, "Package differs from expected SHA-256")
    return package, checksum


def validate_checksum(content, filename, expected):
    lines = content.strip().splitlines()
    require(len(lines) == 1, "Checksum must contain one record")
    match = re.fullmatch(r"([a-fA-F0-9]{64})[ \t]+\*?([^\r\n]+)", lines[0])
    require(match is not None and match[1].lower() == expected and match[2] == filename, "Checksum hash or filename mismatch")


def missing_assets(wanted, existing, draft):
    require(len({asset["name"] for asset in existing}) == len(existing), "Duplicate destination assets")
    expected = {asset["name"]: asset for asset in wanted}
    found = {asset["name"]: asset for asset in existing}
    require(set(found) <= set(expected), "Unexpected destination assets")
    for name, asset in found.items():
        require(asset.get("size") == expected[name]["size"] and digest(asset) == digest(expected[name]), "Destination asset differs; refusing overwrite")
    missing = [asset for asset in wanted if asset["name"] not in found]
    require(draft or not missing, "Published destination is incomplete")
    return missing


def api(path, method="GET", payload=None, missing_ok=False):
    request = urllib.request.Request("https://api.github.com/" + path,
        data=None if payload is None else json.dumps(payload).encode(), method=method,
        headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"],
                 "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                 "Content-Type": "application/json", "User-Agent": "public-release-import"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if missing_ok and error.code == 404:
            return None
        raise RuntimeError(f"GitHub API request failed: HTTP {error.code}") from None


def download(asset, folder):
    path = folder / asset["name"]
    checksum = hashlib.sha256()
    size = 0
    # Public asset requests deliberately have no credentials, including redirects.
    with urllib.request.urlopen(asset["browser_download_url"], timeout=120) as response, path.open("xb") as output:
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            require(size <= asset["size"], "Download exceeds expected size")
            checksum.update(chunk)
            output.write(chunk)
    require(size == asset["size"] and checksum.hexdigest() == digest(asset), "Downloaded asset failed integrity check")
    return path


def main():
    source = repository(os.environ["SOURCE_REPOSITORY"])
    target = repository(os.environ["GITHUB_REPOSITORY"])
    require(source.lower() != target.lower(), "Source and destination must differ")
    tag = os.environ["SOURCE_TAG"]
    expected = os.environ["EXPECTED_SHA256"]
    platform, version, build = version_info(tag)
    encoded_tag = urllib.parse.quote(tag, safe="")
    repo = api(f"repos/{source}")
    release = api(f"repos/{source}/releases/tags/{encoded_tag}")
    assets = validate_source(source, tag, expected, repo, release)
    require(api(f"repos/{target}").get("private") is False, "Destination must be public")
    # Listing includes drafts, avoiding duplicate drafts after an interrupted import.
    releases = api(f"repos/{target}/releases?per_page=100")
    require(len(releases) < 100, "Too many destination releases for bounded lookup")
    matches = [item for item in releases if item["tag_name"] == tag]
    require(len(matches) <= 1, "Ambiguous destination release")
    destination = matches[0] if matches else None
    if destination:
        require(destination["prerelease"] == release["prerelease"], "Destination status differs")
        missing_assets(assets, destination["assets"], destination["draft"])
    with tempfile.TemporaryDirectory(prefix="public-release-") as temporary:
        paths = [download(asset, Path(temporary)) for asset in assets]
        validate_checksum(paths[1].read_text(encoding="utf-8"), assets[0]["name"], expected)
        if destination is None:
            title = f"Doubo {'Android' if platform == 'android' else 'for Windows'} {version}" + (f" ({build})" if build else "")
            instructions = ("Install the APK on a compatible Android device. For updates, install the newer APK over the existing app."
                            if platform == "android" else
                            "Extract the complete ZIP and run Doubo.exe. Keep the bundled files together. For updates, extract the newer package into a separate folder.")
            body = (f"{title}\n\n{instructions}\n\nThis package is mirrored from an existing public release without rebuilding or modifying it. "
                    "Follow your operating system's installation and signature prompts.\n\n"
                    f"SHA-256: `{expected}`\n\nVerify the package against the accompanying `{assets[1]['name']}` checksum file before installing.\n\n"
                    "[Downloads and support](https://doubo.tv/) · [Privacy policy](https://doubo.tv/en/privacy/)")
            destination = api(f"repos/{target}/releases", "POST", {"tag_name": tag, "target_commitish": "main", "name": title,
                "body": body, "draft": True, "prerelease": release["prerelease"]})
        missing = missing_assets(assets, destination["assets"], destination["draft"])
        if missing:
            command = ["gh", "release", "upload", tag, "--repo", target]
            command += [str(path) for path in paths if path.name in {asset["name"] for asset in missing}]
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=600)
        destination = api(f"repos/{target}/releases/{destination['id']}")
        missing_assets(assets, destination["assets"], False)
        require(destination["prerelease"] == release["prerelease"], "Uploaded release status differs")
        if destination["draft"]:
            destination = api(f"repos/{target}/releases/{destination['id']}", "PATCH",
                {"draft": False, "make_latest": "false" if release["prerelease"] else "true"})
        require(destination["draft"] is False, "Release was not published")
        print(f"Verified {tag}: {destination['html_url']}\nSHA-256: {expected}")


if __name__ == "__main__":
    main()
