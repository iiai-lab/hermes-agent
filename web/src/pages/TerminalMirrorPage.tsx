import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Copy, Eye, RefreshCw, ShieldAlert, Terminal } from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Switch } from "@nous-research/ui/ui/components/switch";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { api } from "@/lib/api";
import type {
  TuiObservationSession,
  TuiObservationSnapshot,
  TuiObservationStatus,
} from "@/lib/api";
import { usePageHeader } from "@/contexts/usePageHeader";
import { PluginSlot } from "@/plugins";

const SNAPSHOT_LINES = 80;
const POLL_MS = 2000;

const STATUS_TONE: Partial<Record<TuiObservationStatus, "success" | "warning" | "destructive" | "secondary">> = {
  idle_ready: "success",
  idle_ready_after_activity: "success",
  running: "secondary",
  streaming: "secondary",
  possibly_idle: "warning",
  waiting_for_permission: "warning",
  auth_required: "destructive",
  update_prompt: "warning",
  blocked: "destructive",
  stale: "warning",
  exited: "destructive",
  error: "destructive",
  unknown: "secondary",
};

function formatCapturedAt(seconds?: number): string {
  if (!seconds) return "never";
  return new Date(seconds * 1000).toLocaleTimeString();
}

function statusTone(status?: TuiObservationStatus) {
  if (!status) return "secondary";
  return STATUS_TONE[status] ?? "secondary";
}

function sessionLabel(session: TuiObservationSession): string {
  const name = session.session_name || session.window_name || session.pane_id;
  const kind = session.agent_kind && session.agent_kind !== "unknown" ? session.agent_kind : session.command;
  return `${name} · ${kind || "tmux"}`;
}

export default function TerminalMirrorPage() {
  const { setAfterTitle, setEnd } = usePageHeader();
  const [sessions, setSessions] = useState<TuiObservationSession[]>([]);
  const [selectedPaneId, setSelectedPaneId] = useState<string | null>(null);
  const [snapshot, setSnapshot] = useState<TuiObservationSnapshot | null>(null);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [loadingSessions, setLoadingSessions] = useState(false);
  const [loadingSnapshot, setLoadingSnapshot] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const snapshotRequestSeq = useRef(0);
  const sessionsRequestSeq = useRef(0);
  const inFlightSnapshotPane = useRef<string | null>(null);

  const selectedSession = useMemo(
    () => sessions.find((session) => session.pane_id === selectedPaneId) ?? null,
    [selectedPaneId, sessions],
  );

  const refreshSessions = useCallback(() => {
    const requestSeq = sessionsRequestSeq.current + 1;
    sessionsRequestSeq.current = requestSeq;
    setLoadingSessions(true);
    setError(null);
    api
      .getTuiObservationSessions()
      .then((resp) => {
        if (sessionsRequestSeq.current !== requestSeq) return;
        if (resp.status === "error") {
          setError(resp.error || resp.reason || "tmux list-panes failed");
        }
        setSessions(resp.sessions);
        setSelectedPaneId((currentPaneId) => {
          if (currentPaneId && resp.sessions.some((session) => session.pane_id === currentPaneId)) {
            return currentPaneId;
          }
          return resp.sessions[0]?.pane_id ?? null;
        });
      })
      .catch((err) => {
        if (sessionsRequestSeq.current === requestSeq) {
          setError(String(err));
        }
      })
      .finally(() => {
        if (sessionsRequestSeq.current === requestSeq) {
          setLoadingSessions(false);
        }
      });
  }, []);

  const refreshSnapshot = useCallback(() => {
    const paneId = selectedPaneId;
    if (!paneId || inFlightSnapshotPane.current === paneId) return;
    const requestSeq = snapshotRequestSeq.current + 1;
    snapshotRequestSeq.current = requestSeq;
    inFlightSnapshotPane.current = paneId;
    setLoadingSnapshot(true);
    setError(null);
    api
      .getTuiObservationSnapshot(paneId, SNAPSHOT_LINES)
      .then((nextSnapshot) => {
        if (snapshotRequestSeq.current === requestSeq && nextSnapshot.pane_id === paneId) {
          setSnapshot(nextSnapshot);
        }
      })
      .catch((err) => {
        if (snapshotRequestSeq.current === requestSeq) {
          setError(String(err));
        }
      })
      .finally(() => {
        if (snapshotRequestSeq.current === requestSeq) {
          setLoadingSnapshot(false);
        }
        if (inFlightSnapshotPane.current === paneId) {
          inFlightSnapshotPane.current = null;
        }
      });
  }, [selectedPaneId]);

  useLayoutEffect(() => {
    setAfterTitle(
      <span className="flex items-center gap-2">
        {(loadingSessions || loadingSnapshot) && <Spinner className="shrink-0 text-base text-primary" />}
        <Badge tone={statusTone(snapshot?.status)} className="text-[10px]">
          {snapshot?.status ?? "read-only"}
        </Badge>
        {snapshot && (
          <Badge tone="secondary" className="text-[10px]">
            confidence {(snapshot.confidence * 100).toFixed(0)}%
          </Badge>
        )}
      </span>,
    );
    setEnd(
      <div className="flex w-full min-w-0 flex-wrap items-center justify-end gap-2 sm:gap-3">
        <div className="flex items-center gap-2">
          <Switch
            checked={autoRefresh}
            onCheckedChange={setAutoRefresh}
            id="terminal-mirror-auto-refresh"
          />
          <Label htmlFor="terminal-mirror-auto-refresh" className="cursor-pointer text-xs">
            Poll 2s
          </Label>
          {autoRefresh && (
            <Badge tone="success" className="text-[10px]">
              <span className="mr-1 inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-current" />
              live
            </Badge>
          )}
        </div>
        <Button
          type="button"
          size="sm"
          outlined
          onClick={() => {
            refreshSessions();
            refreshSnapshot();
          }}
          disabled={loadingSessions || loadingSnapshot}
          prefix={loadingSessions || loadingSnapshot ? <Spinner /> : <RefreshCw />}
        >
          Refresh
        </Button>
      </div>,
    );
    return () => {
      setAfterTitle(null);
      setEnd(null);
    };
  }, [
    autoRefresh,
    loadingSessions,
    loadingSnapshot,
    refreshSessions,
    refreshSnapshot,
    setAfterTitle,
    setEnd,
    snapshot,
  ]);

  useEffect(() => {
    snapshotRequestSeq.current += 1;
    inFlightSnapshotPane.current = null;
    setSnapshot(null);
    setLoadingSnapshot(false);
  }, [selectedPaneId]);

  useEffect(() => {
    refreshSessions();
  }, [refreshSessions]);

  useEffect(() => {
    refreshSnapshot();
  }, [refreshSnapshot]);

  useEffect(() => {
    if (!autoRefresh) return;
    const interval = setInterval(() => {
      refreshSessions();
      refreshSnapshot();
    }, POLL_MS);
    return () => clearInterval(interval);
  }, [autoRefresh, refreshSessions, refreshSnapshot]);

  return (
    <div className="flex min-h-0 flex-col gap-4">
      <PluginSlot name="terminal-mirror:top" />

      <div className="grid min-h-0 gap-4 xl:grid-cols-[320px_minmax(0,1fr)]">
        <Card>
          <CardHeader className="px-4 py-3">
            <CardTitle className="flex items-center gap-2 text-sm">
              <Terminal className="h-4 w-4" />
              Tmux panes
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 px-4 pb-4">
            {error && (
              <div className="border border-destructive/20 bg-destructive/10 p-3 text-sm text-destructive">
                {error}
              </div>
            )}

            <div className="flex items-start gap-2 border border-warning/30 bg-warning/10 p-3 text-xs leading-relaxed text-muted-foreground normal-case">
              <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
              <p>
                Read-only mirror. The dashboard captures redacted tmux text and never sends keys or approvals.
              </p>
            </div>

            {sessions.length === 0 && !loadingSessions && (
              <div className="py-8 text-center text-sm text-muted-foreground normal-case">
                No tmux panes found. Launch Claude Code or Codex TUI first, then refresh.
              </div>
            )}

            <div className="flex flex-col gap-2">
              {sessions.map((session) => {
                const selected = session.pane_id === selectedPaneId;
                return (
                  <button
                    key={session.pane_id}
                    type="button"
                    onClick={() => setSelectedPaneId(session.pane_id)}
                    className={`border p-3 text-left transition-colors ${
                      selected
                        ? "border-primary/60 bg-primary/10 text-foreground"
                        : "border-current/10 bg-muted/20 text-muted-foreground hover:border-current/30 hover:text-foreground"
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate text-sm font-medium normal-case">
                        {sessionLabel(session)}
                      </span>
                      <Badge tone={session.dead ? "destructive" : "secondary"} className="text-[10px]">
                        {session.dead ? "dead" : session.pane_id}
                      </Badge>
                    </div>
                    <div className="mt-1 truncate font-mono-ui text-[11px] normal-case">
                      {session.current_path || "—"}
                    </div>
                  </button>
                );
              })}
            </div>
          </CardContent>
        </Card>

        <Card className="min-h-0">
          <CardHeader className="px-4 py-3">
            <CardTitle className="flex min-w-0 items-center justify-between gap-3 text-sm">
              <span className="flex min-w-0 items-center gap-2">
                <Eye className="h-4 w-4" />
                <span className="truncate normal-case">
                  {selectedSession ? sessionLabel(selectedSession) : "Terminal Mirror"}
                </span>
              </span>
              {snapshot && (
                <Badge tone={statusTone(snapshot.status)} className="shrink-0 text-[10px]">
                  {snapshot.status}
                </Badge>
              )}
            </CardTitle>
          </CardHeader>
          <CardContent className="min-h-0 p-0">
            {selectedSession && (
              <div className="grid gap-2 border-b border-current/10 px-4 py-3 text-xs text-muted-foreground normal-case lg:grid-cols-2">
                <div className="min-w-0 truncate">
                  attach: <code className="font-mono-ui text-foreground">{selectedSession.attach_command}</code>
                </div>
                <div className="min-w-0 truncate">
                  capture: <code className="font-mono-ui text-foreground">{selectedSession.capture_command}</code>
                </div>
                <div>reason: {snapshot?.reason ?? "—"}</div>
                <div>captured: {formatCapturedAt(snapshot?.captured_at)}</div>
                <div className="lg:col-span-2">
                  evidence: {snapshot?.evidence?.join(" · ") || "—"}
                </div>
              </div>
            )}

            <pre className="m-0 min-h-[460px] max-h-[calc(100vh-240px)] overflow-auto whitespace-pre-wrap bg-black/70 p-4 font-mono-ui text-xs leading-5 text-[#d8f6d1] normal-case">
              {snapshot?.terminal || (selectedPaneId ? "Capturing tmux pane…" : "Select a tmux pane to mirror.")}
            </pre>

            {snapshot && (
              <div className="flex items-center justify-between gap-2 border-t border-current/10 px-4 py-2 text-xs text-muted-foreground normal-case">
                <span>
                  {snapshot.line_count} lines · redaction {snapshot.redaction.enabled ? "enabled" : "disabled"} · untrusted terminal text
                </span>
                <Button
                  type="button"
                  size="sm"
                  outlined
                  prefix={<Copy />}
                  onClick={() => navigator.clipboard?.writeText(snapshot.terminal)}
                >
                  Copy snapshot
                </Button>
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
