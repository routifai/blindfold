import type * as UseActivitiesModule from "@/hooks/useActivities";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useActivities, useActivity, type Activity } from "@/hooks/useActivities";
import { ActivityPanel } from "./ActivityPanel";

vi.mock("@/hooks/useActivities", async (importOriginal) => ({
  ...(await importOriginal<typeof UseActivitiesModule>()),
  useActivities: vi.fn(),
  useActivity: vi.fn(),
}));

const useActivitiesMock = vi.mocked(useActivities);
const useActivityMock = vi.mocked(useActivity);

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

function activity(overrides: Partial<Activity> & { id: string }): Activity {
  return {
    kind: "turn",
    chat_id: "conv_super",
    title: "Activity",
    outcome: null,
    status: "done",
    started_at: 1_000,
    finished_at: 1_010,
    date: "2026-10-03",
    steps: [],
    ...overrides,
  };
}

describe("ActivityPanel", () => {
  it("renders a loading state while the feed is loading", () => {
    useActivitiesMock.mockReturnValue({ activities: [], isLoading: true, error: null });
    render(<ActivityPanel sessionId="conv_super" />);
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("renders an empty state with no activities", () => {
    useActivitiesMock.mockReturnValue({ activities: [], isLoading: false, error: null });
    render(<ActivityPanel sessionId="conv_super" />);
    expect(screen.getByText("No activity yet.")).toBeInTheDocument();
  });

  it("groups activities by day and renders title/outcome/time", () => {
    useActivitiesMock.mockReturnValue({
      activities: [
        activity({
          id: "turn:conv_super:resp_1",
          title: "Side chat mechanics",
          outcome: "Researched side chat mechanics with quotes",
          date: "2026-10-03",
        }),
        activity({
          id: "sub_agent:conv_child",
          kind: "sub_agent",
          title: "researcher: auth-flow",
          outcome: "Could not retrieve idea details",
          status: "failed",
          date: "2026-10-01",
        }),
      ],
      isLoading: false,
      error: null,
    });
    render(<ActivityPanel sessionId="conv_super" />);

    const dayHeaders = screen.getAllByTestId("activity-day-header");
    expect(dayHeaders.map((el) => el.textContent)).toEqual(["Today", "Oct 1"]);

    expect(screen.getByText("Side chat mechanics")).toBeInTheDocument();
    expect(screen.getByText("Researched side chat mechanics with quotes")).toBeInTheDocument();
    expect(screen.getByText("researcher: auth-flow")).toBeInTheDocument();
    expect(screen.getByText("Could not retrieve idea details")).toBeInTheDocument();
    expect(screen.getByTestId("activity-status-chip")).toHaveTextContent("Failed");
  });

  it("opens the detail view on row click and returns to the feed on back", () => {
    const feedActivity = activity({ id: "turn:conv_super:resp_1", title: "Side chat mechanics" });
    useActivitiesMock.mockReturnValue({
      activities: [feedActivity],
      isLoading: false,
      error: null,
    });
    useActivityMock.mockReturnValue({
      activity: {
        ...feedActivity,
        steps: [{ item_id: "fc1", title: "Searched memory for 'side chat'", created_at: 1_000 }],
      },
      isLoading: false,
      error: null,
    });

    render(<ActivityPanel sessionId="conv_super" />);
    fireEvent.click(screen.getByTestId("activity-row"));

    expect(screen.getByTestId("activity-step-timeline")).toBeInTheDocument();
    expect(screen.getByText("Searched memory for 'side chat'")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("activity-back-button"));
    expect(screen.getByTestId("activity-feed-list")).toBeInTheDocument();
  });
});
