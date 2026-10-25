import type { PhaseStatus } from "@memoryking/contracts";

/** Presentation labels only; lifecycle transitions remain backend-owned. */
export function phaseLabels(phase: PhaseStatus): {
  technical: string;
  delivery: string;
  review: string;
} {
  return {
    technical: phase.technical_status.replaceAll("_", " "),
    delivery:
      phase.email_status === "smtp_accepted"
        ? phase.device_status === "confirmed"
          ? "Kindle receipt confirmed"
          : "Gmail accepted; device unverified"
        : phase.email_status.replaceAll("_", " "),
    review: phase.approval_status,
  };
}

export const graphBudgets = Object.freeze({
  overviewNodes: 200,
  overviewEdges: 400,
  maxNodes: 500,
  maxEdges: 1500,
});
