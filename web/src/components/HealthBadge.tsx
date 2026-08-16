import { useHealth } from '../api/queries';
import { Badge } from '../ui/Badge';
import { Popover } from '../ui/Popover';

/**
 * System state, compressed to one word and expandable to the reasons.
 *
 * The conditions worth interrupting someone for are a model digest that no longer
 * matches the pin (reproducibility is broken, and every result recorded from now
 * on is not the one the audit record claims), a stale schema, a full disk and an
 * unreachable model. They are named individually rather than rolled into
 * "unhealthy" because the operator's next action differs for each.
 */
export function HealthBadge() {
  const health = useHealth();

  if (health.isPending) return <Badge>checking…</Badge>;

  if (health.isError) {
    return (
      <Badge tone="error" title={health.error.message}>
        API unreachable
      </Badge>
    );
  }

  const data = health.data;
  const problems: string[] = [];
  if (!data.model_digest_matches_pin) {
    problems.push('Model digest differs from the config pin — reproducibility is broken.');
  }
  if (!data.migrations_current) problems.push('The database schema is stale.');
  if (!data.disk_ok) problems.push(`Disk nearly full — ${String(data.free_disk_gb)} GB free.`);
  if (!data.llm_reachable) problems.push('The model is not reachable; screening will fail.');

  if (problems.length === 0) {
    return <Badge tone="ok">Healthy · v{data.app_version}</Badge>;
  }

  return (
    <Popover
      trigger={
        <button type="button">
          <Badge tone="warn">{problems.length} system warning(s)</Badge>
        </button>
      }
    >
      <ul className="list-disc space-y-1 pl-4 text-sm">
        {problems.map((problem) => (
          <li key={problem}>{problem}</li>
        ))}
      </ul>
    </Popover>
  );
}
