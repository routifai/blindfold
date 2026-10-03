import { describe, expect, it } from "vitest";
import { activityDayLabel, groupActivitiesByDay, type Activity } from "./useActivities";

function makeActivity(overrides: Partial<Activity>): Activity {
  return {
    id: "turn:conv_1:resp_1",
    kind: "turn",
    chat_id: "conv_1",
    title: "Activity",
    outcome: null,
    status: "done",
    started_at: 0,
    finished_at: 0,
    date: "2026-10-01",
    steps: [],
    ...overrides,
  };
}

describe("groupActivitiesByDay", () => {
  it("groups consecutive same-day activities into one group", () => {
    const activities = [
      makeActivity({ id: "a1", date: "2026-10-03" }),
      makeActivity({ id: "a2", date: "2026-10-03" }),
      makeActivity({ id: "a3", date: "2026-10-01" }),
    ];
    const groups = groupActivitiesByDay(activities);
    expect(groups).toEqual([
      { date: "2026-10-03", activities: [activities[0], activities[1]] },
      { date: "2026-10-01", activities: [activities[2]] },
    ]);
  });

  it("regroups a day that reappears after a different date (non-contiguous input)", () => {
    const activities = [
      makeActivity({ id: "a1", date: "2026-10-03" }),
      makeActivity({ id: "a2", date: "2026-10-01" }),
      makeActivity({ id: "a3", date: "2026-10-03" }),
    ];
    const groups = groupActivitiesByDay(activities);
    // First-seen order of dates is preserved; the later 10-03 item joins
    // the existing 10-03 group rather than starting a second one.
    expect(groups.map((g) => g.date)).toEqual(["2026-10-03", "2026-10-01"]);
    expect(groups[0].activities).toEqual([activities[0], activities[2]]);
    expect(groups[1].activities).toEqual([activities[1]]);
  });

  it("returns an empty array for no activities", () => {
    expect(groupActivitiesByDay([])).toEqual([]);
  });
});

describe("activityDayLabel", () => {
  const now = new Date("2026-10-03T12:00:00Z");

  it("labels today's date as Today", () => {
    expect(activityDayLabel("2026-10-03", now)).toBe("Today");
  });

  it("labels yesterday's date as Yesterday", () => {
    expect(activityDayLabel("2026-10-02", now)).toBe("Yesterday");
  });

  it("labels an older date as a short month/day string", () => {
    expect(activityDayLabel("2026-09-15", now)).toBe("Sep 15");
  });
});
