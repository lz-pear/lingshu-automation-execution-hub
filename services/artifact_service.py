"""执行产物收集与文件服务。"""
import base64
import html
import locale
import mimetypes
import posixpath
import re
import stat
import shutil
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from sqlalchemy import select

from config import ARTIFACT_STORAGE_ROOT, REMOTE_ARTIFACT_ROOT
from database import async_session
from models import ExecutionArtifact

PRIMARY_ARTIFACT_EXTENSIONS = {".html", ".htm", ".docx", ".xlsx"}
HTML_ARTIFACT_EXTENSIONS = {".html", ".htm"}
ARTIFACT_TRASH_DIRNAME = ".trash"
_CSS_URL_PATTERN = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)


def ensure_artifact_storage_root() -> Path:
    ARTIFACT_STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
    return ARTIFACT_STORAGE_ROOT


def get_execution_artifact_dir(execution_id: int) -> Path:
    return ensure_artifact_storage_root() / str(execution_id)


def delete_execution_artifact_dir(execution_id: int):
    path = get_execution_artifact_dir(execution_id).resolve()
    root = ensure_artifact_storage_root().resolve()
    path.relative_to(root)
    if path.exists():
        shutil.rmtree(path)


def _remove_path(path: Path):
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def stage_execution_artifact_dirs(execution_ids: Iterable[int]) -> list[tuple[Path, Path]]:
    root = ensure_artifact_storage_root().resolve()
    trash_root = root / ARTIFACT_TRASH_DIRNAME
    staged: list[tuple[Path, Path]] = []
    try:
        for execution_id in execution_ids:
            source = (root / str(execution_id)).resolve()
            source.relative_to(root)
            if not source.exists():
                continue
            trash_root.mkdir(parents=True, exist_ok=True)
            target = trash_root / f"{execution_id}-{uuid4().hex}"
            source.replace(target)
            staged.append((source, target))
    except Exception:
        restore_staged_artifact_dirs(staged)
        raise
    return staged


def restore_staged_artifact_dirs(staged: Iterable[tuple[Path, Path]]) -> list[Path]:
    failures: list[Path] = []
    for source, target in reversed(list(staged)):
        if not target.exists():
            continue
        try:
            source.parent.mkdir(parents=True, exist_ok=True)
            target.replace(source)
        except OSError:
            failures.append(target)
    return failures


def purge_staged_artifact_dirs(staged: Iterable[tuple[Path, Path]]) -> list[Path]:
    failures: list[Path] = []
    for _, target in staged:
        try:
            _remove_path(target)
        except OSError:
            failures.append(target)
    return failures


def purge_artifact_trash() -> list[Path]:
    trash_root = ensure_artifact_storage_root().resolve() / ARTIFACT_TRASH_DIRNAME
    if not trash_root.exists():
        return []
    failures: list[Path] = []
    for path in trash_root.iterdir():
        try:
            _remove_path(path)
        except OSError:
            failures.append(path)
    if not failures:
        try:
            trash_root.rmdir()
        except OSError:
            pass
    return failures


def ensure_execution_artifact_dir(execution_id: int) -> Path:
    path = get_execution_artifact_dir(execution_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_remote_artifact_dir(execution_id: int) -> str:
    return f"{REMOTE_ARTIFACT_ROOT}/{execution_id}"


def build_execution_env(execution_id: int, artifact_dir: str) -> dict[str, str]:
    return {
        "PLATFORM_EXECUTION_ID": str(execution_id),
        "PLATFORM_ARTIFACT_DIR": artifact_dir,
        "PLATFORM_ARTIFACT_TYPES": "html,docx,xlsx",
    }


def is_primary_artifact(path: Path) -> bool:
    return path.suffix.lower() in PRIMARY_ARTIFACT_EXTENSIONS


def is_previewable_artifact(path: Path) -> bool:
    return path.suffix.lower() in HTML_ARTIFACT_EXTENSIONS


def guess_mime_type(file_name: str) -> str:
    mime_type, _ = mimetypes.guess_type(file_name)
    if mime_type:
        return mime_type
    suffix = Path(file_name).suffix.lower()
    if suffix in HTML_ARTIFACT_EXTENSIONS:
        return "text/html"
    if suffix == ".docx":
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if suffix == ".xlsx":
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return "application/octet-stream"


def iter_execution_artifact_files(execution_id: int) -> Iterable[Path]:
    base_dir = get_execution_artifact_dir(execution_id)
    if not base_dir.exists():
        return []
    return sorted(
        (path for path in base_dir.rglob("*") if path.is_file() and is_primary_artifact(path)),
        key=lambda item: str(item).lower(),
    )


def build_artifact_payload(execution_id: int, file_path: Path, *, source_path: str = "") -> dict:
    resolved_path = file_path.resolve()
    storage_path = str(resolved_path.relative_to(ensure_artifact_storage_root().resolve())).replace("\\", "/")
    return {
        "execution_id": execution_id,
        "file_name": file_path.name,
        "file_ext": file_path.suffix.lower().lstrip("."),
        "mime_type": guess_mime_type(file_path.name),
        "file_size": file_path.stat().st_size,
        "storage_path": storage_path,
        "source_path": source_path,
    }


def collect_local_artifact_payloads(execution_id: int) -> list[dict]:
    return [build_artifact_payload(execution_id, file_path) for file_path in iter_execution_artifact_files(execution_id)]


async def replace_execution_artifacts(execution_id: int, artifacts: list[dict]) -> int:
    async with async_session() as session:
        result = await session.execute(
            select(ExecutionArtifact).where(ExecutionArtifact.execution_id == execution_id)
        )
        for record in result.scalars().all():
            await session.delete(record)

        for artifact in artifacts:
            session.add(ExecutionArtifact(**artifact))

        await session.commit()

    return len(artifacts)


def resolve_storage_path(storage_path: str) -> Path:
    root = ensure_artifact_storage_root().resolve()
    resolved = (root / storage_path).resolve()
    resolved.relative_to(root)
    return resolved


def read_text_file(file_path: Path) -> str:
    raw = file_path.read_bytes()
    encodings = [locale.getpreferredencoding(False), "utf-8", "gbk"]
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def inject_base_href(html_text: str, base_href: str) -> str:
    base_tag = f'<base href="{html.escape(base_href, quote=True)}">'
    lower_text = html_text.lower()
    head_index = lower_text.find("<head")
    if head_index >= 0:
        head_close_index = html_text.find(">", head_index)
        if head_close_index >= 0:
            return f"{html_text[:head_close_index + 1]}{base_tag}{html_text[head_close_index + 1:]}"
    return f"<!DOCTYPE html><html><head>{base_tag}</head><body>{html_text}</body></html>"


def _resolve_html_resource(artifact_dir: Path, base_dir: Path, reference: str) -> tuple[Path, str] | None:
    parsed = urlsplit(html.unescape(reference.strip()))
    if (
        not parsed.path
        or parsed.scheme
        or parsed.netloc
        or parsed.path.startswith(("/", "\\"))
    ):
        return None
    try:
        resource_path = (base_dir / unquote(parsed.path)).resolve()
        resource_path.relative_to(artifact_dir)
    except (OSError, ValueError):
        return None
    if not resource_path.is_file():
        return None
    fragment = f"#{parsed.fragment}" if parsed.fragment else ""
    return resource_path, fragment


def _inline_css_resources(css_text: str, artifact_dir: Path, css_dir: Path, stack: frozenset[Path]) -> str:
    def replace_url(match: re.Match) -> str:
        reference = match.group(2).strip()
        data_uri = _build_resource_data_uri(reference, artifact_dir, css_dir, stack)
        return f'url("{data_uri}")' if data_uri else match.group(0)

    return _CSS_URL_PATTERN.sub(replace_url, css_text)


def _build_resource_data_uri(
    reference: str,
    artifact_dir: Path,
    base_dir: Path,
    stack: frozenset[Path] = frozenset(),
) -> str | None:
    resolved = _resolve_html_resource(artifact_dir, base_dir, reference)
    if not resolved:
        return None
    resource_path, fragment = resolved
    if resource_path in stack:
        return None

    mime_type = guess_mime_type(resource_path.name)
    if mime_type == "text/css":
        css_text = _inline_css_resources(
            read_text_file(resource_path),
            artifact_dir,
            resource_path.parent,
            stack | {resource_path},
        )
        raw = css_text.encode("utf-8")
    else:
        raw = resource_path.read_bytes()
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime_type};base64,{encoded}{fragment}"


def _build_preview_resource_reference(reference: str, artifact_dir: Path, base_dir: Path) -> str | None:
    resolved = _resolve_html_resource(artifact_dir, base_dir, reference)
    if not resolved:
        return None
    resource_path, _ = resolved
    mime_type = guess_mime_type(resource_path.name)
    if not mime_type.startswith("image/"):
        return None
    return reference


class _StandaloneHtmlParser(HTMLParser):
    def __init__(self, artifact_dir: Path):
        super().__init__(convert_charrefs=False)
        self.artifact_dir = artifact_dir
        self.parts: list[str] = []
        self.style_depth = 0
        self.has_image_preview_links = False

    def _inline_attributes(self, tag: str, attrs: list[tuple[str, str | None]]) -> tuple[list[tuple[str, str | None]], bool]:
        changed = False
        updated: list[tuple[str, str | None]] = []
        for name, value in attrs:
            new_value = value
            if value is not None:
                if name in {"src", "poster", "data"}:
                    new_value = _build_resource_data_uri(value, self.artifact_dir, self.artifact_dir)
                    new_value = new_value or value
                elif name == "href" and tag == "link":
                    new_value = _build_resource_data_uri(value, self.artifact_dir, self.artifact_dir)
                    new_value = new_value or value
                elif name == "href" and tag == "a":
                    data_uri = _build_resource_data_uri(value, self.artifact_dir, self.artifact_dir)
                    if data_uri and data_uri.startswith("data:image/"):
                        self.has_image_preview_links = True
                        new_value = "javascript:void(0)"
                        updated.append(("data-preview-src", data_uri))
                        changed = True
                    else:
                        new_value = data_uri or value
                elif name == "srcset":
                    candidates = []
                    for candidate in value.split(","):
                        pieces = candidate.strip().split(maxsplit=1)
                        data_uri = _build_resource_data_uri(pieces[0], self.artifact_dir, self.artifact_dir)
                        candidates.append(" ".join([data_uri or pieces[0], *pieces[1:]]))
                    new_value = ", ".join(candidates)
                elif name == "style":
                    new_value = _inline_css_resources(value, self.artifact_dir, self.artifact_dir, frozenset())
            changed = changed or new_value != value
            updated.append((name, new_value))
        return updated, changed

    @staticmethod
    def _format_tag(tag: str, attrs: list[tuple[str, str | None]], self_closing: bool = False) -> str:
        attributes = "".join(
            f" {name}" if value is None else f' {name}="{html.escape(value, quote=True)}"'
            for name, value in attrs
        )
        ending = " />" if self_closing else ">"
        return f"<{tag}{attributes}{ending}"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        updated, changed = self._inline_attributes(tag, attrs)
        self.parts.append(self._format_tag(tag, updated) if changed else self.get_starttag_text())
        if tag == "style":
            self.style_depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]):
        updated, changed = self._inline_attributes(tag, attrs)
        self.parts.append(self._format_tag(tag, updated, True) if changed else self.get_starttag_text())

    def handle_endtag(self, tag: str):
        if tag == "style" and self.style_depth:
            self.style_depth -= 1
        self.parts.append(f"</{tag}>")

    def handle_data(self, data: str):
        if self.style_depth:
            data = _inline_css_resources(data, self.artifact_dir, self.artifact_dir, frozenset())
        self.parts.append(data)

    def handle_entityref(self, name: str):
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str):
        self.parts.append(f"&#{name};")

    def handle_comment(self, data: str):
        self.parts.append(f"<!--{data}-->")

    def handle_decl(self, decl: str):
        self.parts.append(f"<!{decl}>")

    def handle_pi(self, data: str):
        self.parts.append(f"<?{data}>")


class _PreviewHtmlParser(HTMLParser):
    def __init__(self, artifact_dir: Path):
        super().__init__(convert_charrefs=False)
        self.artifact_dir = artifact_dir
        self.parts: list[str] = []
        self.has_image_preview_links = False

    def _rewrite_attributes(self, tag: str, attrs: list[tuple[str, str | None]]) -> tuple[list[tuple[str, str | None]], bool]:
        changed = False
        updated: list[tuple[str, str | None]] = []
        for name, value in attrs:
            if tag == "a" and name == "href" and value is not None:
                preview_reference = _build_preview_resource_reference(value, self.artifact_dir, self.artifact_dir)
                if preview_reference:
                    self.has_image_preview_links = True
                    updated.append((name, "javascript:void(0)"))
                    updated.append(("data-preview-src", preview_reference))
                    changed = True
                    continue
            updated.append((name, value))
        return updated, changed

    @staticmethod
    def _format_tag(tag: str, attrs: list[tuple[str, str | None]], self_closing: bool = False) -> str:
        attributes = "".join(
            f" {name}" if value is None else f' {name}="{html.escape(value, quote=True)}"'
            for name, value in attrs
        )
        ending = " />" if self_closing else ">"
        return f"<{tag}{attributes}{ending}"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        updated, changed = self._rewrite_attributes(tag, attrs)
        self.parts.append(self._format_tag(tag, updated) if changed else self.get_starttag_text())

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]):
        updated, changed = self._rewrite_attributes(tag, attrs)
        self.parts.append(self._format_tag(tag, updated, True) if changed else self.get_starttag_text())

    def handle_endtag(self, tag: str):
        self.parts.append(f"</{tag}>")

    def handle_data(self, data: str):
        self.parts.append(data)

    def handle_entityref(self, name: str):
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str):
        self.parts.append(f"&#{name};")

    def handle_comment(self, data: str):
        self.parts.append(f"<!--{data}-->")

    def handle_decl(self, decl: str):
        self.parts.append(f"<!{decl}>")

    def handle_pi(self, data: str):
        self.parts.append(f"<?{data}>")


def build_standalone_html(file_path: Path) -> str:
    """把产物目录内的相对资源内嵌为可离线打开的单文件 HTML。"""
    parser = _StandaloneHtmlParser(file_path.parent.resolve())
    parser.feed(read_text_file(file_path))
    parser.close()
    html_text = "".join(parser.parts)
    if parser.has_image_preview_links:
        html_text = _inject_image_preview_overlay(html_text)
    return html_text


def build_preview_html(file_path: Path, base_href: str) -> str:
    """为系统内在线预览构建 HTML，保留相对资源并增强图片点击预览。"""
    parser = _PreviewHtmlParser(file_path.parent.resolve())
    parser.feed(read_text_file(file_path))
    parser.close()
    html_text = inject_base_href("".join(parser.parts), base_href)
    if parser.has_image_preview_links:
        html_text = _inject_image_preview_overlay(html_text)
    return html_text


def _inject_image_preview_overlay(html_text: str) -> str:
    overlay_markup = """
<style>
.artifact-image-preview-overlay{position:fixed;inset:0;background:rgba(0,0,0,.82);display:none;align-items:center;justify-content:center;padding:24px;box-sizing:border-box;z-index:2147483647}
.artifact-image-preview-overlay.is-open{display:flex}
.artifact-image-preview-stage{position:relative;display:flex;align-items:center;justify-content:center;width:100%;height:100%;overflow:hidden;cursor:grab}
.artifact-image-preview-stage.is-dragging{cursor:grabbing}
.artifact-image-preview-overlay img{max-width:none;max-height:none;object-fit:contain;box-shadow:0 12px 40px rgba(0,0,0,.45);background:#fff;transform-origin:center center;user-select:none;-webkit-user-drag:none;cursor:inherit;transition:transform .12s ease-out}
.artifact-image-preview-close{position:fixed;top:18px;right:22px;z-index:2;border:0;border-radius:999px;background:rgba(255,255,255,.18);color:#fff;width:40px;height:40px;font-size:28px;line-height:40px;cursor:pointer}
</style>
<div class="artifact-image-preview-overlay" id="artifact-image-preview-overlay" aria-hidden="true">
  <button type="button" class="artifact-image-preview-close" id="artifact-image-preview-close" aria-label="关闭预览">&times;</button>
  <div class="artifact-image-preview-stage" id="artifact-image-preview-stage">
    <img id="artifact-image-preview-image" alt="预览图片">
  </div>
</div>
<script>
(function(){
  var overlay=document.getElementById("artifact-image-preview-overlay");
  var stage=document.getElementById("artifact-image-preview-stage");
  var image=document.getElementById("artifact-image-preview-image");
  var closeButton=document.getElementById("artifact-image-preview-close");
  if(!overlay||!stage||!image||!closeButton){return;}
  var currentScale=1;
  var fitScale=1;
  var translateX=0;
  var translateY=0;
  var dragging=false;
  var startX=0;
  var startY=0;
  var startTranslateX=0;
  var startTranslateY=0;
  function clampScale(scale){
    return Math.min(Math.max(scale,fitScale*0.5),8);
  }
  function applyTransform(){
    image.style.transform="translate("+translateX+"px,"+translateY+"px) scale("+currentScale+")";
  }
  function resetPosition(){
    translateX=0;
    translateY=0;
    applyTransform();
  }
  function fitToViewport(){
    if(!image.naturalWidth||!image.naturalHeight){return;}
    var stageRect=stage.getBoundingClientRect();
    var availableWidth=Math.max(stageRect.width-24,120);
    var availableHeight=Math.max(stageRect.height-24,120);
    fitScale=Math.min(availableWidth/image.naturalWidth,availableHeight/image.naturalHeight,1);
    currentScale=fitScale;
    resetPosition();
  }
  function setScale(nextScale,anchorX,anchorY){
    if(!image.naturalWidth||!image.naturalHeight){return;}
    var bounded=clampScale(nextScale);
    var rect=stage.getBoundingClientRect();
    var centerX=(anchorX===undefined?rect.left+rect.width/2:anchorX)-rect.left-rect.width/2;
    var centerY=(anchorY===undefined?rect.top+rect.height/2:anchorY)-rect.top-rect.height/2;
    var ratio=bounded/currentScale;
    translateX=centerX-(centerX-translateX)*ratio;
    translateY=centerY-(centerY-translateY)*ratio;
    currentScale=bounded;
    applyTransform();
  }
  function openPreview(src){
    image.setAttribute("src",src);
    image.onload=function(){
      fitToViewport();
      image.onload=null;
    };
    overlay.classList.add("is-open");
    overlay.setAttribute("aria-hidden","false");
  }
  function requestParentPreview(src){
    if(window.parent===window){return false;}
    try{
      var resolved=new URL(src,document.baseURI).href;
      window.parent.postMessage({type:"artifact-image-preview-request",src:resolved},"*");
      return true;
    }catch(error){
      return false;
    }
  }
  function closePreview(){
    overlay.classList.remove("is-open");
    overlay.setAttribute("aria-hidden","true");
    image.removeAttribute("src");
    image.style.transform="";
    stage.classList.remove("is-dragging");
    dragging=false;
  }
  document.addEventListener("click",function(event){
    var trigger=event.target.closest("a[data-preview-src]");
    if(!trigger){return;}
    event.preventDefault();
    var previewSrc=trigger.getAttribute("data-preview-src");
    if(requestParentPreview(previewSrc)){return;}
    openPreview(previewSrc);
  });
  closeButton.addEventListener("click",closePreview);
  overlay.addEventListener("click",function(event){
    if(event.target===overlay){closePreview();}
  });
  stage.addEventListener("pointerdown",function(event){
    if(event.button!==0||!overlay.classList.contains("is-open")){return;}
    dragging=true;
    startX=event.clientX;
    startY=event.clientY;
    startTranslateX=translateX;
    startTranslateY=translateY;
    stage.classList.add("is-dragging");
    if(stage.setPointerCapture){stage.setPointerCapture(event.pointerId);}
  });
  stage.addEventListener("pointermove",function(event){
    if(!dragging){return;}
    translateX=startTranslateX+(event.clientX-startX);
    translateY=startTranslateY+(event.clientY-startY);
    applyTransform();
  });
  function stopDragging(event){
    if(event&&stage.releasePointerCapture){
      try{stage.releasePointerCapture(event.pointerId);}catch(error){}
    }
    dragging=false;
    stage.classList.remove("is-dragging");
  }
  stage.addEventListener("pointerup",stopDragging);
  stage.addEventListener("pointercancel",stopDragging);
  stage.addEventListener("wheel",function(event){
    if(!overlay.classList.contains("is-open")){return;}
    event.preventDefault();
    setScale(currentScale*(event.deltaY<0?1.12:1/1.12),event.clientX,event.clientY);
  },{passive:false});
  stage.addEventListener("dblclick",function(event){
    if(currentScale<=fitScale*1.05){
      setScale(Math.max(fitScale*2,1.6),event.clientX,event.clientY);
      return;
    }
    fitToViewport();
  });
  document.addEventListener("keydown",function(event){
    if(event.key==="Escape"){closePreview();}
  });
  window.addEventListener("resize",function(){
    if(overlay.classList.contains("is-open")){fitToViewport();}
  });
})();
</script>
"""
    body_close_index = html_text.lower().rfind("</body>")
    if body_close_index >= 0:
        return f"{html_text[:body_close_index]}{overlay_markup}{html_text[body_close_index:]}"
    return f"{html_text}{overlay_markup}"


def download_sftp_tree(sftp, remote_root: str, local_root: Path) -> list[Path]:
    copied_files: list[Path] = []
    local_root.mkdir(parents=True, exist_ok=True)

    def walk(remote_dir: str, local_dir: Path):
        try:
            entries = sftp.listdir_attr(remote_dir)
        except OSError:
            return

        local_dir.mkdir(parents=True, exist_ok=True)
        for entry in entries:
            remote_path = posixpath.join(remote_dir, entry.filename)
            local_path = local_dir / entry.filename
            if stat.S_ISDIR(entry.st_mode):
                walk(remote_path, local_path)
            else:
                local_path.parent.mkdir(parents=True, exist_ok=True)
                sftp.get(remote_path, str(local_path))
                copied_files.append(local_path)

    walk(remote_root, local_root)
    return copied_files
