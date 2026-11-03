// Generated from inspection-v1.schema.json. Do not edit.
export interface History {
  project: string;
  recorded_position?: number;
}

export interface Project {
  project: string;
}

export interface Retention {
  project: string;
  observation: string;
}

export interface Purge {
  project: string;
  observation: string;
  preview: string;
  confirm: string;
}

export interface Influence {
  project: string;
  observation: string;
}

export interface TrustRule {
  project: string;
  source: string;
  level: "trusted" | "untrusted";
  explicit?: boolean;
}

export interface Release {
  project: string;
  observation: string;
}

export interface Catalog {
  project: string;
  kind?: "observations" | "claims";
  source?: string;
  name?: string;
  id?: string;
  after?: number;
  limit?: number;
  text?: boolean;
}

export interface Health {
  project: string;
  days?: number;
}

export interface Inspection0 {
  version: 1;
  incarnation: string;
  action: "history";
  args: History;
}

export interface Inspection1 {
  version: 1;
  incarnation: string;
  action: "operations";
  args: Project;
}

export interface Inspection2 {
  version: 1;
  incarnation: string;
  action: "cost";
  args: Project;
}

export interface Inspection3 {
  version: 1;
  incarnation: string;
  action: "handoffs";
  args: Project;
}

export interface Inspection4 {
  version: 1;
  incarnation: string;
  action: "retention_preview";
  args: Retention;
}

export interface Inspection5 {
  version: 1;
  incarnation: string;
  action: "purge";
  args: Purge;
  key: string;
}

export interface Inspection6 {
  version: 1;
  incarnation: string;
  action: "influence";
  args: Influence;
}

export interface Inspection7 {
  version: 1;
  incarnation: string;
  action: "trust";
  args: TrustRule;
  key: string;
}

export interface Inspection8 {
  version: 1;
  incarnation: string;
  action: "release";
  args: Release;
  key: string;
}

export interface Inspection9 {
  version: 1;
  incarnation: string;
  action: "trust_list";
  args: Project;
}

export interface Inspection10 {
  version: 1;
  incarnation: string;
  action: "held";
  args: Project;
}

export interface Inspection11 {
  version: 1;
  incarnation: string;
  action: "catalog";
  args: Catalog;
}

export interface Inspection12 {
  version: 1;
  incarnation: string;
  action: "health";
  args: Health;
}

export type InspectionCommand = Inspection0 | Inspection1 | Inspection2 | Inspection3 | Inspection4 | Inspection5 | Inspection6 | Inspection7 | Inspection8 | Inspection9 | Inspection10 | Inspection11 | Inspection12;
