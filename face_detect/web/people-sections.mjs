export function peopleSection(group){
  if(group.person_id!=null)return 'people';
  return group.section|| (group.optional_outlier?'other':'review');
}
