import test from 'node:test'
import assert from 'node:assert/strict'
import { formatDecimal, presentationValue } from '../src/decimal-display.ts'
test('decimal text rounds exactly, preserves identifiers and clears negative zero',()=>{
  for(const [input,expected] of [['2.584','2.58'],['23.196','23.2'],['1.005','1.01'],['-1.005','-1.01'],['-.005','-0.01'],['-0.004','0'],['0.0000','0'],['999999999999999999.995','1000000000000000000'],['1.2345e-3','0'],['1.239e2','123.9'],['001731910000000001','001731910000000001']]) assert.equal(formatDecimal(input),expected)
})
test('nested presentation is rounded without mutating evidence or IDs',()=>{
 const input={id:'1738120406431008374',sku:'0123.456',source_row_ids:['001.2345'],amount:'23.196',rows:[{itemId:'003.4567',profit:'0.005532877073673364'}]}
 assert.deepEqual(presentationValue(input),{...input,amount:'23.2',rows:[{...input.rows[0],profit:'0.01'}]})
 assert.equal(input.amount,'23.196')
})
