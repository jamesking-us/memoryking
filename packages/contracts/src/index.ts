import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { Ajv2020 } from "ajv/dist/2020.js";
import { parseTree, type Node, type ParseError } from "jsonc-parser";
export type * from "./generated.js";
export type * from "./retrieval-generated.js";

const MAX_BYTES = 1_048_576;
const MAX_DEPTH = 64;
const schema = JSON.parse(
  readFileSync(new URL("./schema.json", import.meta.url), "utf8"),
);
const ajv = new Ajv2020({ strict: false, allErrors: false });
ajv.addSchema(schema);
const retrievalSchema = JSON.parse(
  readFileSync(new URL("./retrieval.schema.json", import.meta.url), "utf8"),
);
const retrievalValidator = ajv.compile(retrievalSchema);
export class ContractError extends Error {}

function checkString(value: string): void {
  for (const char of value) {
    const point = char.codePointAt(0)!;
    if (point >= 0xd800 && point <= 0xdfff)
      throw new ContractError("invalid_unicode");
  }
}

export function loadStrict(raw: string): unknown {
  checkString(raw);
  if (Buffer.byteLength(raw, "utf8") > MAX_BYTES)
    throw new ContractError("document_too_large");
  const errors: ParseError[] = [];
  let tree: Node | undefined;
  try {
    tree = parseTree(raw, errors, {
      disallowComments: true,
      allowTrailingComma: false,
    });
  } catch {
    throw new ContractError("invalid_json");
  }
  if (!tree || errors.length) throw new ContractError("invalid_json");
  function inspect(node: Node, depth: number): void {
    if (depth > MAX_DEPTH) throw new ContractError("document_too_deep");
    if (node.type === "object") {
      const seen = new Set<string>();
      for (const property of node.children ?? []) {
        const [key, value] = property.children!;
        checkString(key.value);
        if (seen.has(key.value)) throw new ContractError("duplicate_key");
        seen.add(key.value);
        inspect(value, depth + 1);
      }
    } else if (node.type === "array") {
      for (const child of node.children ?? []) inspect(child, depth + 1);
    } else if (node.type === "number") {
      if (/[.eE]/.test(raw.slice(node.offset, node.offset + node.length))) {
        throw new ContractError("integer_profile_required");
      }
      if (!Number.isSafeInteger(node.value))
        throw new ContractError("unsafe_integer");
    } else if (node.type === "string") checkString(node.value);
  }
  inspect(tree, 0);
  return JSON.parse(raw);
}

function scalarCompare(a: string, b: string): number {
  const aa = Array.from(a, (char) => char.codePointAt(0)!);
  const bb = Array.from(b, (char) => char.codePointAt(0)!);
  for (let i = 0; i < Math.min(aa.length, bb.length); i++) {
    if (aa[i] !== bb[i]) return aa[i] - bb[i];
  }
  return aa.length - bb.length;
}

export function canonicalBytes(value: unknown): Buffer {
  function encode(input: unknown, depth: number): string {
    if (depth > MAX_DEPTH) throw new ContractError("document_too_deep");
    if (input === null || typeof input === "boolean")
      return JSON.stringify(input);
    if (typeof input === "number") {
      if (!Number.isSafeInteger(input))
        throw new ContractError("unsafe_integer");
      return JSON.stringify(input);
    }
    if (typeof input === "string") {
      checkString(input);
      return JSON.stringify(input);
    }
    if (Array.isArray(input))
      return (
        "[" + Array.from(input, (v) => encode(v, depth + 1)).join(",") + "]"
      );
    if (
      typeof input === "object" &&
      Object.getPrototypeOf(input) === Object.prototype
    ) {
      return (
        "{" +
        Object.keys(input)
          .sort(scalarCompare)
          .map((key) => {
            checkString(key);
            return (
              JSON.stringify(key) +
              ":" +
              encode((input as Record<string, unknown>)[key], depth + 1)
            );
          })
          .join(",") +
        "}"
      );
    }
    throw new ContractError("unsupported_value");
  }
  const result = Buffer.from(encode(value, 0), "utf8");
  if (result.length > MAX_BYTES) throw new ContractError("document_too_large");
  return result;
}

export function canonicalHash(value: unknown): string {
  return createHash("sha256").update(canonicalBytes(value)).digest("hex");
}

export function validateContract(name: string, value: unknown): void {
  canonicalBytes(value);
  if (!Object.hasOwn(schema.$defs, name))
    throw new ContractError("unknown_contract");
  const validate = ajv.getSchema(schema.$id + "#/$defs/" + name)!;
  if (!validate(value)) throw new ContractError("schema_invalid");
}

export function validateRetrieval(value: unknown): void {
  canonicalBytes(value);
  if (!retrievalValidator(value)) throw new ContractError("schema_invalid");
}

export type { WorkingCommand } from "./working-generated.js";
const workingValidator = ajv.compile(
  JSON.parse(
    readFileSync(new URL("./working.schema.json", import.meta.url), "utf8"),
  ),
);
export function validateWorking(value: unknown): void {
  canonicalBytes(value);
  if (!workingValidator(value)) throw new ContractError("schema_invalid");
}

export type { HandoffCommand } from "./handoff-generated.js";
const handoffValidator = ajv.compile(
  JSON.parse(
    readFileSync(new URL("./handoff.schema.json", import.meta.url), "utf8"),
  ),
);
export function validateHandoff(value: unknown): void {
  canonicalBytes(value);
  if (!handoffValidator(value)) throw new ContractError("schema_invalid");
}

export type { InferenceCommand } from "./inference-generated.js";
const inferenceValidator = ajv.compile(
  JSON.parse(
    readFileSync(new URL("./inference.schema.json", import.meta.url), "utf8"),
  ),
);
export function validateInference(value: unknown): void {
  canonicalBytes(value);
  if (!inferenceValidator(value)) throw new ContractError("schema_invalid");
}

export type { RepositoryCommand } from "./repository-generated.js";
const repositoryValidator = ajv.compile(
  JSON.parse(
    readFileSync(new URL("./repository.schema.json", import.meta.url), "utf8"),
  ),
);
export function validateRepository(value: unknown): void {
  canonicalBytes(value);
  if (!repositoryValidator(value)) throw new ContractError("schema_invalid");
}

export type { EmbeddingCommand } from "./embedding-generated.js";
const embeddingValidator = ajv.compile(
  JSON.parse(
    readFileSync(new URL("./embedding.schema.json", import.meta.url), "utf8"),
  ),
);
export function validateEmbedding(value: unknown): void {
  canonicalBytes(value);
  if (!embeddingValidator(value)) throw new ContractError("schema_invalid");
}

export type { ProcessingCommand } from "./processing-generated.js";
const processingValidator = ajv.compile(
  JSON.parse(
    readFileSync(new URL("./processing.schema.json", import.meta.url), "utf8"),
  ),
);
export function validateProcessing(value: unknown): void {
  canonicalBytes(value);
  if (!processingValidator(value)) throw new ContractError("schema_invalid");
}

export type { ExperienceCommand } from "./experience-generated.js";
const experienceValidator = ajv.compile(
  JSON.parse(
    readFileSync(new URL("./experience.schema.json", import.meta.url), "utf8"),
  ),
);
export function validateExperience(value: unknown): void {
  canonicalBytes(value);
  if (!experienceValidator(value)) throw new ContractError("schema_invalid");
}

export type * from "./viewer-generated.js";
export type * from "./reporting-generated.js";
