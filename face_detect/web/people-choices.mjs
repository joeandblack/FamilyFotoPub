export function personChoices(state){
 const choices=new Map();
 for(const person of [...(state?.people||[]),...(state?.groups||[])]){
  if(person.person_id&&person.name&&!choices.has(person.person_id))choices.set(person.person_id,person);
 }
 return [...choices.values()];
}
