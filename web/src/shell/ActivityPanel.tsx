// Activity Feed tab content — a self-fetching right-rail panel listing
// every Activity for a Super Chat / Side Chat session (rollover/CONTEXT.md
// "Activity Feed"), grouped by day. Clicking a row opens the Activity's
// detail view: a status chip, a left-column timeline of its Steps in
// plain language, and a right column showing the selected Step's detail.
// Mirrors the list + detail layout conventions of SubagentsPanel.tsx and
// CommentsPanel.tsx.

import { useEffect, useState, type ReactNode } from "react";
import { BotIcon, ChevronLeftIcon, MessageCircleIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  activityDayLabel,
  groupActivitiesByDay,
  useActivities,
  useActivity,
  type Activity,
  type ActivityStatus,
  type ActivityStep,
} from "@/hooks/useActivities";
import { cn } from "@/lib/utils";

export interface ActivityPanelProps {
  /** A Super Chat or Side Chat session id — the Activity Feed covers its
   *  whole family (itself, its Side Chats, and their Sub-agents). */
  sessionId: string;
}

const STATUS_LABEL: Record<ActivityStatus, string> = {
  in_progress: "In Progress",
  done: "Done",
  failed: "Failed",
  cancelled: "Cancelled",
};

// Token-based tones (no custom colors): destructive for Failed, the
// shared warning amber for In Progress (an active, attention-worthy
// state), a quiet blue for Done (an expected outcome), and neutral grey
// for Cancelled.
const STATUS_BADGE_CLASS: Record<ActivityStatus, string> = {
  in_progress: "bg-warning/15 text-warning",
  done: "bg-session-active/15 text-session-active",
  failed: "bg-destructive/10 text-destructive",
  cancelled: "bg-muted text-muted-foreground",
};

function StatusChip({ status }: { status: ActivityStatus }) {
  return (
    <Badge
      data-testid="activity-status-chip"
      className={cn("border-transparent", STATUS_BADGE_CLASS[status])}
    >
      {STATUS_LABEL[status]}
    </Badge>
  );
}

function formatActivityTime(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toLocaleTimeString(undefined, {
    hour: "numeric",
    minute: "2-digit",
  });
}

function ActivityKindIcon({ kind }: { kind: Activity["kind"] }) {
  const Icon = kind === "sub_agent" ? BotIcon : MessageCircleIcon;
  return <Icon className="size-3.5 shrink-0 text-muted-foreground" />;
}

function PanelHeader({ children }: { children: ReactNode }) {
  return <div className="flex h-11 shrink-0 items-center gap-1 border-b px-2">{children}</div>;
}

function EmptyState({ children }: { children: ReactNode }) {
  return (
    <div className="flex flex-1 items-center justify-center px-4 py-8 text-center text-sm text-muted-foreground">
      {children}
    </div>
  );
}

/**
 * The Activity Feed: every Activity for ``sessionId``'s Super Chat family,
 * grouped by day ("Today", "Yesterday", "Oct 1"), newest-first. Selecting a
 * row switches this panel into the Activity's detail view.
 */
export function ActivityPanel({ sessionId }: ActivityPanelProps) {
  const [selectedActivityId, setSelectedActivityId] = useState<string | null>(null);
  const { activities, isLoading, error } = useActivities(sessionId);

  if (selectedActivityId !== null) {
    return (
      <ActivityDetailView
        sessionId={sessionId}
        activityId={selectedActivityId}
        onBack={() => setSelectedActivityId(null)}
      />
    );
  }

  if (isLoading && activities.length === 0) {
    return (
      <div className="flex h-full min-h-0 flex-col bg-card">
        <PanelHeader>
          <h2 className="font-medium text-ui">Activity</h2>
        </PanelHeader>
        <EmptyState>Loading…</EmptyState>
      </div>
    );
  }
  if (error && activities.length === 0) {
    return (
      <div className="flex h-full min-h-0 flex-col bg-card">
        <PanelHeader>
          <h2 className="font-medium text-ui">Activity</h2>
        </PanelHeader>
        <EmptyState>Failed to load activity.</EmptyState>
      </div>
    );
  }

  const groups = groupActivitiesByDay(activities);

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-card">
      <PanelHeader>
        <h2 className="font-medium text-ui">Activity</h2>
      </PanelHeader>
      {groups.length === 0 ? (
        <EmptyState>No activity yet.</EmptyState>
      ) : (
        <ul
          className="flex min-h-0 flex-1 flex-col overflow-y-auto pb-1"
          data-testid="activity-feed-list"
        >
          {groups.map((group) => (
            <li key={group.date}>
              <div
                className="px-2.5 py-1.5 text-xs font-medium text-muted-foreground"
                data-testid="activity-day-header"
              >
                {activityDayLabel(group.date)}
              </div>
              <ul>
                {group.activities.map((activity) => (
                  <li key={activity.id}>
                    <button
                      type="button"
                      data-testid="activity-row"
                      onClick={() => setSelectedActivityId(activity.id)}
                      className="flex w-full flex-col gap-0.5 px-2.5 py-2 text-left hover:bg-accent/60"
                    >
                      <div className="flex w-full items-center gap-1.5">
                        <ActivityKindIcon kind={activity.kind} />
                        <span className="shrink-0 truncate text-sm font-medium">
                          {activity.title}
                        </span>
                        <span className="flex-1" />
                        {activity.status === "failed" && <StatusChip status="failed" />}
                        <span className="shrink-0 text-xs text-muted-foreground">
                          {formatActivityTime(activity.started_at)}
                        </span>
                      </div>
                      {activity.outcome && (
                        <p className="truncate pl-[20px] text-sm text-muted-foreground">
                          {activity.outcome}
                        </p>
                      )}
                    </button>
                  </li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function StepDetail({ step }: { step: ActivityStep | null }) {
  if (step === null) {
    return <p className="p-2.5 text-sm text-muted-foreground">Select a step to see its detail.</p>;
  }
  if (!step.detail) {
    return <p className="p-2.5 text-sm text-muted-foreground">No detail recorded for this step.</p>;
  }
  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-2.5" data-testid="activity-step-detail">
      <p className="mb-2 text-sm font-medium">{step.title}</p>
      <pre className="whitespace-pre-wrap break-words text-xs text-muted-foreground">
        {JSON.stringify(step.detail, null, 2)}
      </pre>
    </div>
  );
}

/**
 * One Activity's detail view: status chip + title in the header, a left
 * column timing-ordered Step timeline, and a right column with the
 * selected Step's full detail (call arguments / result, capped).
 */
export function ActivityDetailView({
  sessionId,
  activityId,
  onBack,
}: {
  sessionId: string;
  activityId: string;
  onBack: () => void;
}) {
  const { activity, isLoading, error } = useActivity(sessionId, activityId);
  const [selectedStepId, setSelectedStepId] = useState<string | null>(null);

  useEffect(() => {
    if (activity && activity.steps.length > 0 && selectedStepId === null) {
      setSelectedStepId(activity.steps[0].item_id);
    }
  }, [activity, selectedStepId]);

  const selectedStep = activity?.steps.find((step) => step.item_id === selectedStepId) ?? null;

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-card">
      <PanelHeader>
        <Button
          variant="ghost"
          size="icon-xs"
          onClick={onBack}
          aria-label="Back to Activity Feed"
          data-testid="activity-back-button"
        >
          <ChevronLeftIcon className="size-3.5" />
        </Button>
        <h2 className="truncate font-medium text-ui">{activity?.title ?? "Activity"}</h2>
        <span className="flex-1" />
        {activity && <StatusChip status={activity.status} />}
      </PanelHeader>
      {isLoading && !activity ? (
        <EmptyState>Loading…</EmptyState>
      ) : error && !activity ? (
        <EmptyState>Could not load this activity.</EmptyState>
      ) : activity ? (
        <div className="flex min-h-0 flex-1">
          <ul
            className="w-1/2 min-h-0 flex-1 overflow-y-auto border-r pb-1"
            data-testid="activity-step-timeline"
          >
            {activity.steps.length === 0 ? (
              <li className="px-2.5 py-3 text-sm text-muted-foreground">No steps recorded.</li>
            ) : (
              activity.steps.map((step) => (
                <li key={step.item_id}>
                  <button
                    type="button"
                    data-testid="activity-step-row"
                    onClick={() => setSelectedStepId(step.item_id)}
                    className={cn(
                      "flex w-full flex-col gap-0.5 px-2.5 py-2 text-left hover:bg-accent/60",
                      step.item_id === selectedStepId && "bg-accent",
                    )}
                  >
                    <span className="truncate text-sm">{step.title}</span>
                    <span className="text-xs text-muted-foreground">
                      {formatActivityTime(step.created_at)}
                    </span>
                  </button>
                </li>
              ))
            )}
          </ul>
          <div className="w-1/2 min-h-0 flex-1 overflow-y-auto">
            <StepDetail step={selectedStep} />
          </div>
        </div>
      ) : null}
    </div>
  );
}
