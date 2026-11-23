export function reviewSnapshot(groups, ids, includeFaces=false) {
  const selected = ids.map(id => groups.find(group => group.id === id));
  // Older servers keep the global-revision safeguard until they are upgraded.
  if (selected.some(group => !group?.review_version)) return {};
  return {
    group_versions: Object.fromEntries(selected.map(group => [String(group.id), group.review_version])),
    ...(includeFaces?{group_faces:Object.fromEntries(selected.map(group=>[String(group.id),group.faces.map(face=>face.detection_id)]))}:{}),
  };
}
