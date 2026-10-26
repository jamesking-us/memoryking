const identities = [
    "store",
    "tenant",
    "principal",
    "incarnation",
    "authorization_epoch",
    "session_generation",
];
export function sameAuthority(a, b) {
    return identities.every((key) => a[key] === b[key]);
}
export const identifier = (value) => typeof value === "string" && /^[A-Za-z0-9_-]{1,96}$/.test(value);
// This checks the release envelope, not the truth of the source content. Source
// values are rendered inertly and the server owns authoritative DTO validation.
export function envelope(raw, kind, project) {
    if (!raw || typeof raw !== "object")
        throw new Error("invalid_response");
    const value = raw;
    const b = value.binding;
    if (value.schema_version !== 1 ||
        !b ||
        !value.data ||
        value.data.kind !== kind ||
        !identifier(b.store) ||
        !identifier(b.tenant) ||
        !identifier(b.principal) ||
        !identifier(b.incarnation) ||
        b.project !== project ||
        !identifier(b.project) ||
        ![
            b.authorization_epoch,
            b.session_generation,
            b.observed_position,
            b.evaluated_ms,
            b.fresh_until_ms,
        ].every((n) => Number.isSafeInteger(n) && n >= 0) ||
        b.fresh_until_ms <= b.evaluated_ms ||
        typeof value.coverage !== "string" ||
        typeof value.truncated !== "boolean" ||
        !(value.next_cursor === null ||
            (typeof value.next_cursor === "string" &&
                value.next_cursor.length <= 8192)))
        throw new Error("invalid_response");
    if (value.data.kind === "session" &&
        (!Array.isArray(value.data.projects) ||
            value.data.projects.length > 1024 ||
            !value.data.projects.every(identifier) ||
            !Number.isSafeInteger(value.data.poll_seconds) ||
            value.data.poll_seconds < 5 ||
            value.data.poll_seconds > 60 ||
            !/^[a-f0-9]{64}$/.test(value.data.csrf)))
        throw new Error("invalid_response");
    return value;
}
export class Freshness {
    clock;
    cleared;
    generation = 0;
    sequence = 0;
    lastMono;
    lastWall;
    lease = null;
    constructor(clock, cleared) {
        this.clock = clock;
        this.cleared = cleared;
        this.lastMono = clock.monotonic();
        this.lastWall = clock.wall();
    }
    now() {
        const monotonic = this.clock.monotonic(), wall = this.clock.wall();
        if (!Number.isFinite(monotonic) ||
            !Number.isFinite(wall) ||
            monotonic < this.lastMono ||
            wall < this.lastWall) {
            this.clear();
            // Keep the high-water mark: repeated attempts cannot turn rollback into freshness.
            throw new Error("clock_changed");
        }
        this.lastMono = monotonic;
        this.lastWall = wall;
        return { monotonic, wall };
    }
    clear() {
        this.generation++;
        this.sequence++;
        this.lease = null;
        this.cleared();
    }
    startCycle() {
        this.sequence++;
        return this.sequence;
    }
    ticket(sequence) {
        const now = this.now();
        return { generation: this.generation, sequence, ...now };
    }
    current(ticket) {
        return (ticket.generation === this.generation && ticket.sequence === this.sequence);
    }
    receive(ticket, binding) {
        const now = this.now();
        if (!this.current(ticket))
            throw new Error("superseded");
        // Date is a conservative sleep check only. A clock correction never adds time.
        const elapsed = Math.max(now.monotonic - ticket.monotonic, now.wall - ticket.wall);
        const remaining = Math.min(15000, binding.fresh_until_ms - binding.evaluated_ms) - elapsed;
        if (remaining <= 0)
            throw new Error("expired");
        return { monotonic: now.monotonic + remaining, wall: now.wall + remaining };
    }
    commit(ticket, leases) {
        const now = this.now();
        if (!this.current(ticket) || !leases.length)
            throw new Error("superseded");
        const lease = {
            monotonic: Math.min(...leases.map((x) => x.monotonic)),
            wall: Math.min(...leases.map((x) => x.wall)),
        };
        if (lease.monotonic <= now.monotonic || lease.wall <= now.wall)
            throw new Error("expired");
        this.lease = lease;
    }
    remaining() {
        const now = this.now();
        if (!this.lease)
            return 0;
        return Math.max(0, Math.min(this.lease.monotonic - now.monotonic, this.lease.wall - now.wall));
    }
    check() {
        try {
            if (this.remaining() > 0)
                return true;
        }
        catch {
            return false;
        }
        this.clear();
        return false;
    }
}
export function retryDelay(failures, retryAfter, random, wall) {
    let required = 0;
    if (retryAfter !== null) {
        required = /^\d+$/.test(retryAfter)
            ? Number(retryAfter) * 1000
            : Date.parse(retryAfter) - wall;
        if (Number.isNaN(required) || required < 0)
            required = 0;
    }
    const jitter = Math.min(60000, 10000 * 2 ** Math.min(failures, 3)) *
        (0.8 + Math.min(1, Math.max(0, random())) * 0.2);
    // The local exponential backoff caps at 60s; a server's longer Retry-After wins.
    return Math.max(required, jitter);
}
