import React, { useEffect, useRef, useState } from "react";
import Papa from "papaparse";
import { Code, HtmlPreview, Markdown } from "./renderers.jsx";

export function Csv({ text }) {
  const [limit, setLimit] = useState(100);
  const { data } = Papa.parse(text, { skipEmptyLines: true });
  return (
    <div className="archive-table">
      <table>
        <tbody>
          {data.slice(0, limit).map((row, i) => (
            <tr key={i}>
              {row.map((cell, j) =>
                i === 0 ? <th key={j}>{cell}</th> : <td key={j}>{cell}</td>,
              )}
            </tr>
          ))}
        </tbody>
      </table>
      {data.length > limit && (
        <button onClick={() => setLimit(limit + 100)}>
          Show next 100 rows · {data.length} total
        </button>
      )}
    </div>
  );
}
function Pdf({ url }) {
  const canvas = useRef(null),
    [doc, setDoc] = useState(null),
    [page, setPage] = useState(1),
    [error, setError] = useState("");
  useEffect(() => {
    let task,
      active = true;
    Promise.all([
      import("pdfjs-dist"),
      import("pdfjs-dist/build/pdf.worker.min.mjs?url"),
    ])
      .then(([pdf, worker]) => {
        if (!active) return;
        pdf.GlobalWorkerOptions.workerSrc = worker.default;
        task = pdf.getDocument({
          url,
          isEvalSupported: false,
          disableAutoFetch: true,
          disableStream: true,
        });
        return task.promise.then((value) => {
          if (active) setDoc(value);
        });
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
      task?.destroy();
    };
  }, [url]);
  useEffect(() => {
    if (!doc) return;
    let task,
      active = true;
    doc
      .getPage(page)
      .then((value) => {
        if (!active) return;
        const viewport = value.getViewport({ scale: 1.4 });
        const element = canvas.current;
        element.width = viewport.width;
        element.height = viewport.height;
        task = value.render({
          canvasContext: element.getContext("2d"),
          viewport,
        });
        return task.promise;
      })
      .catch((e) => {
        if (active && e.name !== "RenderingCancelledException")
          setError(e.message);
      });
    return () => {
      active = false;
      task?.cancel();
    };
  }, [doc, page]);
  return (
    <div className="archive-pdf">
      {error ? (
        <p role="alert">{error}</p>
      ) : (
        <>
          <nav>
            <button
              disabled={!doc || page === 1}
              onClick={() => setPage(page - 1)}
            >
              Previous page
            </button>
            <span>{doc ? `${page} / ${doc.numPages}` : "Loading PDF…"}</span>
            <button
              disabled={!doc || page === doc.numPages}
              onClick={() => setPage(page + 1)}
            >
              Next page
            </button>
          </nav>
          <canvas ref={canvas} aria-label={`PDF page ${page}`} />
        </>
      )}
    </div>
  );
}
function Document({ url, mime }) {
  const [text, setText] = useState(null),
    [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    fetch(url, { signal: controller.signal })
      .then((r) => {
        if (!r.ok)
          throw new Error(
            "Attachment is unavailable or outside this source’s workspace",
          );
        return r.text();
      })
      .then(setText)
      .catch((e) => {
        if (e.name !== "AbortError") setError(e.message);
      });
    return () => controller.abort();
  }, [url]);
  if (error) return <p className="text-caption text-muted">{error}</p>;
  if (text === null)
    return <p className="text-caption text-muted">Loading document…</p>;
  if (mime === "text/html" || mime === "image/svg+xml")
    return <HtmlPreview html={text} />;
  if (mime === "text/csv") return <Csv text={text} />;
  if (mime === "text/markdown") return <Markdown text={text} />;
  return (
    <Code
      text={text}
      language={mime === "application/json" ? "json" : "text"}
    />
  );
}
function Attachment({ asset, source, part }) {
  const [error, setError] = useState(false);
  const url = `/api/transcripts/${source}/media?${new URLSearchParams({ line: part.line, index: part.index, asset: asset.asset })}`;
  const mime = asset.mime;
  let preview;
  if (error)
    preview = (
      <p className="text-caption text-muted">
        File unavailable in the current source workspace.
      </p>
    );
  else if (asset.origin === "external")
    preview = (
      <p className="text-caption text-muted">
        External media ·{" "}
        <a href={asset.reference} target="_blank" rel="noreferrer noopener">
          Open reference
        </a>
      </p>
    );
  else if (mime.startsWith("image/") && mime !== "image/svg+xml")
    preview = (
      <a href={url} target="_blank" rel="noreferrer">
        <img
          loading="lazy"
          src={url}
          alt={asset.name}
          onError={() => setError(true)}
        />
      </a>
    );
  else if (mime.startsWith("audio/"))
    preview = (
      <audio
        controls
        preload="metadata"
        src={url}
        onError={() => setError(true)}
      />
    );
  else if (mime.startsWith("video/"))
    preview = (
      <video
        controls
        preload="metadata"
        src={url}
        onError={() => setError(true)}
      />
    );
  else if (mime === "application/pdf") preview = <Pdf url={url} />;
  else if (
    mime.startsWith("text/") ||
    mime === "image/svg+xml" ||
    mime === "application/json"
  )
    preview = <Document url={url} mime={mime} />;
  else
    preview = (
      <p className="text-caption text-muted">
        {asset.reference ||
          "Attachment bytes or preview format were not retained."}
      </p>
    );
  return (
    <figure className="archive-attachment">
      <figcaption>
        {asset.name}
        <span>
          {asset.origin === "local reference"
            ? "Current file · may differ from rollout"
            : asset.origin === "embedded"
              ? "Retained in source"
              : asset.mime}
        </span>
      </figcaption>
      {preview}
    </figure>
  );
}
export function Attachments({ part, source }) {
  return (
    <>
      {(part.media || []).map((asset) => (
        <Attachment
          key={asset.asset}
          asset={asset}
          source={source}
          part={part}
        />
      ))}
    </>
  );
}
