/**
 * Maps path-flow artifact `role` values to short UI labels.
 * Linked / intermediate functions use "Chain", not "context".
 */
export function displayChainRoleLabel(role: string | undefined): string {
  const r = (role ?? '').toLowerCase()
  if (r === 'target') return 'Leaf'
  if (r === 'context') return 'Chain'
  if (!role?.trim()) return 'Chain'
  return role.replace(/_/g, ' ')
}
