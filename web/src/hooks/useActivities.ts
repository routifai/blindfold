import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";

/** One Step within an Activity, in plain language. */
export interface ActivityStep {
  item_id: string;
  title: string;
  created_at: number;
  tool?: string;
  /** Call arguments + result, capped. Populated only by ``useActivity`` (the
   *  detail fetch) — ``useActivities`` (the feed list) omits it. */
  detail?: { call: Record<string, unknown>; result?: Record<string, unknown> };
}

export type ActivityStatus = "in_progress" | "done" | "failed" | "cancelled";

/**
 * One Activity, as returned by the Activity Feed routes.
 *
 * Mirrors ``activity_to_dict`` in ``omnigent/superchat/activity.py``.
 */
export interface Activity {
  id: string;
  kind: "turn" | "sub_agent";
  chat_id: string;
  title: string;
  outcome: string | null;
  status: ActivityStatus;
  started_at: number;
  finished_at: number | null;
  /** UTC calendar day (``"YYYY-MM-DD"``), for day-grouping the feed. */
  date: string;
  steps: ActivityStep[];
}

interface ActivityListResponse {
  object: "list";
  data: Activity[];
}

/**
 * Fetch the Activity Feed for a Super Chat / Side Chat session.
 *
 * Exported for unit testing of the HTTP-shape contract; production code
 * should call ``useActivities``.
 */
export async function fetchActivities(
  sessionId: string,
  opts?: { before?: number; limit?: number },
): Promise<Activity[]> {
  const params = new URLSearchParams();
  if (opts?.before != null) params.set("before", String(opts.before));
  if (opts?.limit != null) params.set("limit", String(opts.limit));
  const query = params.toString();
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/activities${query ? `?${query}` : ""}`,
  );
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const json = (await res.json()) as ActivityListResponse;
  return json.data;
}

/** Fetch one Activity in full (steps include capped call/result detail). */
export async function fetchActivity(sessionId: string, activityId: string): Promise<Activity> {
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/activities/${encodeURIComponent(activityId)}`,
  );
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as Activity;
}

interface UseActivitiesResult {
  activities: Activity[];
  isLoading: boolean;
  error: Error | null;
}

/** Stable TanStack Query key for a session's Activity Feed. */
export function activitiesQueryKey(sessionId: string): readonly unknown[] {
  return ["conversation", sessionId, "activities"];
}

/**
 * The Activity Feed for one Super Chat / Side Chat session, served by
 * ``GET /v1/sessions/{id}/activities``.
 *
 * @param sessionId - A Super Chat or Side Chat session id, or ``null`` to
 *   disable the query.
 */
export function useActivities(sessionId: string | null): UseActivitiesResult {
  const { data, isLoading, error } = useQuery({
    queryKey:
      sessionId === null ? ["conversation", null, "activities"] : activitiesQueryKey(sessionId),
    queryFn: () => fetchActivities(sessionId as string),
    enabled: sessionId !== null,
    staleTime: 30_000,
    retry: false,
  });
  return {
    activities: data ?? [],
    isLoading,
    error: (error as Error | null) ?? null,
  };
}

interface UseActivityResult {
  activity: Activity | null;
  isLoading: boolean;
  error: Error | null;
}

/**
 * One Activity in full, served by
 * ``GET /v1/sessions/{id}/activities/{activityId}``.
 *
 * @param sessionId - A Super Chat or Side Chat session id, or ``null``.
 * @param activityId - The Activity to open, or ``null`` to disable the query.
 */
export function useActivity(
  sessionId: string | null,
  activityId: string | null,
): UseActivityResult {
  const { data, isLoading, error } = useQuery({
    queryKey: ["conversation", sessionId, "activity", activityId],
    queryFn: () => fetchActivity(sessionId as string, activityId as string),
    enabled: sessionId !== null && activityId !== null,
    staleTime: 30_000,
    retry: false,
  });
  return {
    activity: data ?? null,
    isLoading,
    error: (error as Error | null) ?? null,
  };
}

/** One day's worth of Activities, newest-first within the day. */
export interface ActivityDayGroup {
  date: string;
  activities: Activity[];
}

/**
 * Group Activities by their ``date`` field, preserving input order within
 * each day and across days (the server already returns newest-first).
 *
 * Pure function — no fetching — so the Activity Feed panel's day-grouping
 * logic is unit-testable without mounting React or mocking the network.
 *
 * @param activities - Activities in the order to preserve, e.g. the feed's
 *   newest-first server order.
 * @returns One group per distinct ``date``, in first-seen order.
 */
export function groupActivitiesByDay(activities: Activity[]): ActivityDayGroup[] {
  const groups: ActivityDayGroup[] = [];
  const indexByDate = new Map<string, number>();
  for (const activity of activities) {
    let index = indexByDate.get(activity.date);
    if (index === undefined) {
      index = groups.length;
      indexByDate.set(activity.date, index);
      groups.push({ date: activity.date, activities: [] });
    }
    groups[index].activities.push(activity);
  }
  return groups;
}

/**
 * Render a group's date as "Today" / "Yesterday" / ``"Oct 1"``, matching the
 * Muse reference UX's feed-section headers.
 *
 * @param isoDate - The group's ``"YYYY-MM-DD"`` date (UTC calendar day).
 * @param now - Current time, injectable for tests; defaults to ``new Date()``.
 */
export function activityDayLabel(isoDate: string, now: Date = new Date()): string {
  const today = now.toISOString().slice(0, 10);
  const yesterday = new Date(now.getTime() - 24 * 60 * 60 * 1000).toISOString().slice(0, 10);
  if (isoDate === today) return "Today";
  if (isoDate === yesterday) return "Yesterday";
  const [year, month, day] = isoDate.split("-").map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  return date.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
}
