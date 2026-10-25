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

export type InspectionCommand = Inspection0 | Inspection1 | Inspection2 | Inspection3 | Inspection4 | Inspection5;
