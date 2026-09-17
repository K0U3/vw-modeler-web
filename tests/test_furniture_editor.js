// DOM-independent checks of the editor's Python serialization and validation.
// node tests/test_furniture_editor.js fixture.json edited.py
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const path = require('path');
class Element {
  constructor() { this.value=''; this.children=[]; this.style={}; this.handlers={}; this.textContent=''; }
  addEventListener(type, fn) { this.handlers[type]=fn; }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren() { this.children=[]; }
  setAttribute() {}
  add(option) { if (!this.children.length) this.value=option.value; this.children.push(option); }
  querySelector() { return pre; }
}
const pre=new Element(), elements=new Map();
const document={getElementById(id) { if(!elements.has(id))elements.set(id,new Element());return elements.get(id); },createElement(){return new Element();}};
const sandbox={document, Option:function(text,value){this.textContent=text;this.value=value;},console};
vm.createContext(sandbox);
const html=fs.readFileSync(path.join(__dirname,'../frontend/index.html'),'utf8');
vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1],sandbox);
const fixture=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
sandbox.fixture=fixture;
vm.runInContext('lastScript = fixture.script; renderFurnitureEdit(fixture.summary);',sandbox);
assert(vm.runInContext('furnitureRows.length > 0',sandbox));
assert(vm.runInContext('applyFurnitureSettings()',sandbox));
vm.runInContext(`furnitureCatalog.push({name:'記号"\\\\部品 $& ', label:'test',category:'test',w:600,d:800,h:900,z0:0,cx:10,cy:20});
 furnitureRows[0].select.value = String(furnitureCatalog.length-1);
 furnitureRows[0].angle.value='180'; furnitureRows[0].dx.value='-50'; furnitureRows[0].dy.value='25';`,sandbox);
assert(vm.runInContext('applyFurnitureSettings()',sandbox));
const valid = vm.runInContext('lastScript',sandbox);
vm.runInContext("furnitureRows[0].dx.value='';",sandbox);
assert.strictEqual(vm.runInContext('applyFurnitureSettings()',sandbox),false);
assert.strictEqual(vm.runInContext('lastScript',sandbox),valid);
vm.runInContext("furnitureRows[0].dx.value='-50'; furnitureRows[1].select.value='skip';",sandbox);
assert(vm.runInContext('applyFurnitureSettings()',sandbox));
fs.writeFileSync(process.argv[3],vm.runInContext('lastScript',sandbox));
console.log('PASS furniture editor selection, angle, movement, skip, invalid input, escaping');
