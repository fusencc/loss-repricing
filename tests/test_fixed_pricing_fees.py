"""固定表与直发公式的业务回归；全部输入为临时合成数据。"""
import copy
import json
from decimal import Decimal, localcontext
from pathlib import Path
from zipfile import ZipFile
from xml.etree.ElementTree import Element, SubElement, tostring

import pytest

from app.adapters.fixed_pricing_fees import import_workbook, select_fixed_rate, SCHEMA
from app.adapters.sales_fee_model import compile_direct_fee
from app.config import Settings


def workbook(path):
    rates = {'B2':'VAT费率', 'C12':'总费率(%)'}
    for row, site in enumerate(['MY','PH','TH','VN'],3):
        rates.update({f'{col}{row}':'0' for col in 'BCDE'})
        rates[f'A{row}']=site
    rates.update(B3='10',B5='需要填对应类目的税费')
    for row, site in enumerate(['VN','TH','PH','MY'],13):
        rates.update({f'{col}{row}':'0.0001' for col in 'CDEFGHJKMNOPQRSTU'})
        rates[f'A{row}']=site
    for row in range(23,39):
        rates.update({f'{col}{row}':'0' for col in 'CDEFG'})
        rates[f'A{row}']='部门'+str((row-23)//4)
        rates[f'B{row}']=['TH','VN','MY','PH'][(row-23)%4]
    for row in [53,56,60,62]:
        rates.update({f'{col}{row}':'10' for col in 'EFG'})
    products={'G4':'SHOPTH','I4':'P1','J4':'I1','L4':'SKU','M4':'5','V4':'7'}
    ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    with ZipFile(path,'w') as z:
        book=Element('workbook',xmlns=ns);sheets=SubElement(book,'sheets')
        rels=Element('Relationships',xmlns='http://schemas.openxmlformats.org/package/2006/relationships')
        for i,(name,cells) in enumerate([('费率表',rates),('BD项目',products)],1):
            SubElement(sheets,'sheet',name=name,**{'{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id':f'rId{i}'})
            SubElement(rels,'Relationship',Id=f'rId{i}',Target=f'worksheets/sheet{i}.xml')
            sheet=Element('worksheet',xmlns=ns);data=SubElement(sheet,'sheetData')
            for ref,value in cells.items():
                cell=SubElement(data,'c',r=ref,t='inlineStr')
                SubElement(SubElement(cell,'is'),'t').text=value
            if name=='BD项目':
                # 一个过期公式缓存，不能当成第二条固定佣金输入。
                for ref in ['G5','I5','J5','L5']:
                    cell=SubElement(data,'c',r=ref,t='inlineStr')
                    SubElement(SubElement(cell,'is'),'t').text=products[ref[:-1]+'4']
                cell=SubElement(data,'c',r='M5');SubElement(cell,'f').text='10+20';SubElement(cell,'v').text='30'
            z.writestr(f'xl/worksheets/sheet{i}.xml',tostring(sheet))
        z.writestr('xl/workbook.xml',tostring(book))
        z.writestr('xl/_rels/workbook.xml.rels',tostring(rels))


def test_import_keeps_raw_fixed_inputs_and_rejects_formula_caches(tmp_path):
    file=tmp_path/'fixed.xlsx';workbook(file)
    config=import_workbook(file)
    assert config['public']['MY']['vatRate']=='10'
    assert config['public']['TH']['vatRate'] is None
    assert config['overseas']['PH']['vatRate']=='0.0001'
    assert config['overseas']['PH']['orderProcessFee']=='0.0001'
    assert 'product_inputs' not in config
    assert len(config['source']['sha256'])==64
    assert config['version'].endswith(config['source']['sha256'][:16])


def direct_source():
    variant={'currency':'PHP','overseasStockSum':0,'marketPrice':'100','localWeight':'21','costPrice':'2'}
    rate={'siteCode':'PH','version':'fixed-v1','vatRate':'0',
          'procedureRate':'2.24','commissionRate':'8.5','returnRate':'8','otherRate':'0.001',
          'marketingRate':'10','packageFee':'0.3','warehouseCost':'1.3','fixedFreight':'0',
          'freight':{'base_weight_g':'10','base_price':'5','per_10g_price':'4.5'}}
    return variant,rate


def test_direct_formula_retains_platform_commission_without_product_commission():
    variant,rate=direct_source()
    result=compile_direct_fee(variant,rate,site='PH',company_cny_per_currency='0.1')
    # 21g => 首10g 5PHP + 两档4.5PHP = 14PHP；2+.3+1.3+1.4=5CNY。
    assert Decimal(result['proof']['freight_station'])==Decimal('14')
    assert result['online_model'].profit(Decimal('100'))==Decimal('21.259')
    assert 'model' not in result


@pytest.mark.parametrize('weight,freight',[('10','5'),('10.01','9.5'),('20','9.5'),('20.01','14')])
def test_direct_freight_exact_boundaries(weight,freight):
    variant,rate=direct_source();variant['localWeight']=weight
    result=compile_direct_fee(variant,rate,site='PH',company_cny_per_currency='0.1')
    assert Decimal(result['proof']['freight_station'])==Decimal(freight)


def test_direct_freight_tier_does_not_depend_on_global_decimal_precision():
    variant,rate=direct_source();variant['localWeight']='20.000000000000000001'
    with localcontext() as context:
        context.prec=3
        result=compile_direct_fee(variant,rate,site='PH',company_cny_per_currency='0.1')
    assert Decimal(result['proof']['freight_station'])==Decimal('14')


@pytest.mark.parametrize('change',['missing_weight','missing_cost','nonzero_freight','missing_vat'])
def test_direct_missing_inputs_never_become_zero(change):
    variant,rate=direct_source()
    if change=='missing_weight':variant.pop('localWeight')
    elif change=='missing_cost':variant.pop('costPrice')
    elif change=='nonzero_freight':rate['fixedFreight']='1'
    else:rate['vatRate']=None
    with pytest.raises((ValueError,TypeError,KeyError)):
        compile_direct_fee(variant,rate,site='PH',company_cny_per_currency='0.1')


def test_fixed_rates_do_not_require_product_membership_and_th_needs_online_category_vat(tmp_path):
    workbook(tmp_path/'fixed.xlsx');config=import_workbook(tmp_path/'fixed.xlsx')
    config['departments']['D1']=config['departments'].pop('部门0')
    source={'schema_version':SCHEMA,'source_id':'FIXED','config':config,'acquisition_complete':True}
    variant={'sellerSkuId':'I1','localSellerSku':'SKU'}
    parent={'shopName':'SHOPTH','productId':'P1','department':'D1',
            'taxVatRate':'24.12','tkCategoryIdLeaf':'600747'}
    rate=select_fixed_rate(source,variant,parent,'TH','direct')
    assert rate['vatRate']=='24.12'
    assert 'publicCommissionRate' not in rate
    assert rate['vat_source']['category_id']=='600747'
    assert select_fixed_rate(source,dict(variant,sellerSkuId='NEW'),dict(parent,shopName='OTHER'),'TH','direct')['vatRate']=='24.12'
    with pytest.raises(ValueError):select_fixed_rate(source,variant,dict(parent,department='OTHER'),'TH','direct')
    parent['taxVatRate']=None
    with pytest.raises(ValueError,match='VAT'):select_fixed_rate(source,variant,parent,'TH','direct')
    assert select_fixed_rate(source,variant,parent,'TH','overseas')['vatRate']=='0.0001'


@pytest.mark.parametrize('value',[None, '', True, '-1', 'NaN', 'Infinity'])
def test_th_online_vat_never_falls_back_to_fixed_product_input(tmp_path, value):
    workbook(tmp_path/'fixed.xlsx');config=import_workbook(tmp_path/'fixed.xlsx')
    source={'source_id':'FIXED','config':config,'acquisition_complete':True}
    variant={'sellerSkuId':'I1','localSellerSku':'SKU'}
    parent={'shopName':'SHOPTH','productId':'P1','department':'部门0',
            'taxVatRate':value,'tkCategoryIdLeaf':'600747'}
    with pytest.raises(ValueError,match='VAT'):
        select_fixed_rate(source,variant,parent,'TH','direct')


def test_th_zero_vat_is_valid_but_category_is_required(tmp_path):
    workbook(tmp_path/'fixed.xlsx');config=import_workbook(tmp_path/'fixed.xlsx')
    source={'source_id':'FIXED','config':config,'acquisition_complete':True}
    variant={'sellerSkuId':'I1','localSellerSku':'SKU'}
    parent={'shopName':'SHOPTH','productId':'P1','department':'部门0',
            'taxVatRate':'0','tkCategoryIdLeaf':'600747'}
    assert select_fixed_rate(source,variant,parent,'TH','direct')['vatRate']=='0'
    parent['tkCategoryIdLeaf']=None
    with pytest.raises(ValueError,match='类目'):
        select_fixed_rate(source,variant,parent,'TH','direct')


def test_th_overseas_keeps_fixed_vat_without_online_vat(tmp_path):
    workbook(tmp_path/'fixed.xlsx');config=import_workbook(tmp_path/'fixed.xlsx')
    source={'source_id':'FIXED','config':config,'acquisition_complete':True}
    variant={'sellerSkuId':'I1','localSellerSku':'SKU'}
    parent={'shopName':'SHOPTH','productId':'P1','department':'部门0'}
    rate=select_fixed_rate(source,variant,parent,'TH','overseas')
    assert rate['vatRate']=='0.0001' and 'vat_source' not in rate


def test_direct_extra_public_commission_is_not_a_formula_input():
    variant,rate=direct_source()
    rate['publicCommissionRate']='5'
    with_field=compile_direct_fee(variant,rate,site='PH',company_cny_per_currency='0.1')
    rate.pop('publicCommissionRate')
    without_field=compile_direct_fee(variant,rate,site='PH',company_cny_per_currency='0.1')
    assert with_field['online_model']==without_field['online_model']
    assert without_field['online_model'].revenue_factor==Decimal('0.71259')
