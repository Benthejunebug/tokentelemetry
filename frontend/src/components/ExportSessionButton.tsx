"use client";

/**
 * Download a session's source from the trace header.
 *
 * The backend streams the bytes rather than handing over a path, because a path
 * only helps when TokenTelemetry runs on the same machine as the agent. In a
 * container the source is mounted at a container path that means nothing on the
 * host — and may not be mounted at all, which is the `unavailable` case below.
 *
 * Three honest outcomes, never collapsed into one:
 *   file / files  the real source, byte for byte (several are zipped)
 *   serialized    no file exists (rows in a shared DB) — a labelled rebuild
 *   unavailable   nothing readable from here, with the reason and the fix
 */

import { useEffect, useState } from "react";
import { Download, AlertTriangle, FileWarning } from "lucide-react";
import { Button } from "@/components/ui";
import { apiFetch, sessionExportUrl, type SessionExportInfo } from "@/lib/api";

function formatBytes(n: number | null): string {
  if (!n) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export default function ExportSessionButton({
  sessionId,
  agent,
}: {
  sessionId: string;
  agent: string;
}) {
  const [info, setInfo] = useState<SessionExportInfo | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    apiFetch(`/sessions/${encodeURIComponent(sessionId)}/export-info?agent=${encodeURIComponent(agent)}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (!cancelled) setInfo(d); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [sessionId, agent]);

  if (!info) return null;

  const unavailable = info.kind === "unavailable";
  const reconstructed = info.kind === "serialized";

  return (
    <div className="relative">
      <Button
        variant="secondary"
        size="md"
        disabled={unavailable}
        title={
          unavailable
            ? info.reason ?? "No exportable source"
            : `Download ${info.filename}${info.bytes ? ` (${formatBytes(info.bytes)})` : ""}`
        }
        onClick={() => {
          if (unavailable) return;
          // A plain navigation, not fetch(): the browser handles the save, and
          // the token rides in the query string since a download can't set
          // an Authorization header.
          window.location.href = sessionExportUrl(sessionId, agent);
        }}
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => setOpen(false)}
      >
        {unavailable ? <FileWarning size={14} /> : <Download size={14} />}
        Export
      </Button>

      {open && (
        <div className="absolute right-0 top-full mt-1 z-50 w-[320px] rounded-[var(--tt-radius)] border border-[var(--tt-border)] bg-[var(--tt-panel)] p-3 shadow-lg text-[11px] leading-relaxed">
          {unavailable ? (
            <>
              <div className="flex items-center gap-1.5 font-semibold text-[var(--tt-fg)]">
                <AlertTriangle size={12} className="text-amber-400" />
                No source to export
              </div>
              <div className="mt-1 text-[var(--tt-fg-muted)]">{info.reason}</div>
              {info.hint && <div className="mt-1.5 text-[var(--tt-fg-dim)]">{info.hint}</div>}
            </>
          ) : (
            <>
              <div className="font-mono text-[var(--tt-fg)] break-all">{info.filename}</div>
              {reconstructed ? (
                <div className="mt-1.5 text-[var(--tt-fg-muted)]">
                  {agent} keeps sessions as rows in a shared database, so there is
                  no single source file. This download is a reconstruction of this
                  session&apos;s rows, and says so inside.
                </div>
              ) : (
                <>
                  <div className="mt-1.5 text-[var(--tt-fg-dim)]">
                    {info.paths.length > 1
                      ? `${info.paths.length} files, zipped — read from:`
                      : "Copied byte for byte from:"}
                  </div>
                  {info.paths.map((p) => (
                    <div key={p} className="font-mono text-[10px] text-[var(--tt-fg-muted)] break-all">
                      {p}
                    </div>
                  ))}
                </>
              )}
              <div className="mt-2 border-t border-[var(--tt-border)] pt-1.5 text-[var(--tt-fg-dim)]">
                Contains absolute paths, cwd, and any tool output from the run.
                Check it before attaching to an issue.
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
