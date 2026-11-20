"""BD表显式导入及统一费用字段转换；真实运行仅复用导入的物流价卡。"""
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import json
import zipfile
import xml.etree.ElementTree as ET

from app.adapters.normalization import money

SCHEMA = 'user-fixed-pricing-fees-v2'
NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def import_workbook(path):
    """只采用固定输入单元格；不使用BD商品页的公开计划佣金、公式缓存、利润率或历史库存。"""
    path = Path(path)
    raw = path.read_bytes()
    with zipfile.ZipFile(path) as archive:
        strings = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            strings = [''.join(node.itertext()) for node in ET.fromstring(archive.read('xl/sharedStrings.xml'))]
        book = ET.fromstring(archive.read('xl/workbook.xml'))
        relations = {node.attrib['Id']: node.attrib['Target'] for node in
                     ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))}
        sheets = {}
        for sheet in book.find('m:sheets', NS):
            relation = sheet.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id']
            target = relations[relation]
            target = target.lstrip('/') if target.startswith('/') else 'xl/' + target
            cells = {}
            for cell in ET.fromstring(archive.read(target)).findall('.//m:c', NS):
                value = cell.find('m:v', NS)
                text = value.text if value is not None else None
                if cell.get('t') == 's' and text is not None:
                    text = strings[int(text)]
                elif cell.get('t') == 'inlineStr':
                    text = ''.join(cell.find('m:is', NS).itertext())
                # 本次配置导入不计算公式，所有被采用的固定输入必须是原始值。
                if cell.find('m:f', NS) is None and cell.get('t') != 'e' and text is not None:
                    cells[cell.attrib['r']] = text
            sheets[sheet.attrib['name']] = cells
    rates = sheets['费率表']
    if rates.get('B2') != 'VAT费率' or rates.get('C12') != '总费率(%)':
        raise ValueError('固定费率表布局与已核实版本不一致')
    config = {'schema_version': SCHEMA, 'version': 'user-fixed-no-public-commission-v2-' + sha256(raw).hexdigest()[:16],
        'source': {'file': str(path.resolve()), 'sha256': sha256(raw).hexdigest(),
                   'imported_at': datetime.now(timezone.utc).isoformat()},
        'public': {}, 'overseas': {}, 'departments': {}, 'freight': {}}

    def number(cells, key, *, nullable=False):
        try:
            value = money(cells[key])
            if value < 0:
                raise ValueError('负费率或费用')
            return str(value)
        except (KeyError, ValueError, ArithmeticError):
            if nullable:
                return None
            raise ValueError('固定费率单元格无有效非负数：' + key) from None

    for row in range(3, 7):
        site = rates['A' + str(row)]
        config['public'][site] = {name: number(rates, col + str(row), nullable=name == 'vatRate')
            for col, name in [('B', 'vatRate'), ('C', 'procedureRate'), ('D', 'commissionRate'), ('E', 'warehouseCost')]}
        config['public'][site]['source_cells'] = f'费率表!A{row}:E{row}'
    columns = dict(zip('CDEFGHJKMNOPQRSTU', [
        'totalRate', 'totalCost', 'costOfCapital', 'procedureRate', 'platformRate', 'vatRate',
        'returnRate', 'warehouseStorageRate', 'marketingRate', 'otherRate', 'packageFee',
        'warehouseCost', 'warehouseCostDomestic', 'overseaPackageFee', 'publishFee', 'dischargeFee', 'orderProcessFee']))
    for row in range(13, 17):
        site = rates['A' + str(row)]
        config['overseas'][site] = {name: number(rates, col + str(row)) for col, name in columns.items()}
        config['overseas'][site].update(siteCode=site, source_cells=f'费率表!A{row}:U{row}')
    for row in range(23, 39):
        department, site = rates['A' + str(row)], rates['B' + str(row)]
        config['departments'].setdefault(department, {})[site] = {name: number(rates, col + str(row))
            for col, name in [('C', 'returnRate'), ('D', 'marketingRate'), ('E', 'otherRate'),
                              ('F', 'packageFee'), ('G', 'fixedFreight')]}
    # 重量单位g、价格为站点币。费用公式AE用跨境物流E:G，不使用含买家运费的I:K。
    for site, row in [('TH', 53), ('VN', 56), ('MY', 60), ('PH', 62)]:
        config['freight'][site] = {name: number(rates, col + str(row)) for col, name in
                                   [('E', 'base_weight_g'), ('F', 'base_price'), ('G', 'per_10g_price')]}
    return config


def select_fixed_rate(source, variant, parent, site, mode):
    if not source.get('acquisition_complete') or not isinstance(source.get('config'), dict):
        raise ValueError('用户固定费率配置未导入或无效')
    config = source['config']
    try:
        if mode == 'overseas':
            rate = dict(config['overseas'][site])
        else:
            rate = {**config['public'][site], **config['departments'][parent['department']][site]}
            rate['freight'] = config['freight'][site]
            if site == 'TH':
                # 2026-11-16官方AIBP查询实测未提供VAT。复用本轮在线父商品
                # findByPage.taxVatRate（百分数），不增加逐SKU查询或回退旧V列。
                # 父子身份已由prepare_online_states核对，税率必须带尾类目证据。
                category = parent.get('tkCategoryIdLeaf')
                if not isinstance(category, str) or not category.strip():
                    raise ValueError('泰国直发VAT缺少当前商品尾类目')
                try:
                    vat = money(parent.get('taxVatRate'))
                    if vat < 0:
                        raise ValueError()
                except (ValueError, ArithmeticError):
                    raise ValueError('泰国直发VAT缺失或无效，不能使用旧表或填0') from None
                rate['vatRate'] = str(vat)
                rate['vat_source'] = {'contract_version': 'sales-parent-category-vat-20261116-v1',
                    'kind': 'sales_web_parent', 'path': '/Api/api-tiktok-sales/online/findByPage',
                    'field': 'taxVatRate', 'unit': 'percent', 'category_id': category,
                    'shop_name': parent['shopName'], 'product_id': parent['productId']}
            if rate['vatRate'] is None:
                raise ValueError('固定表未明确本站点的直发VAT')
        return {**rate, 'siteCode': site, 'version': config['version'],
                'source_ref': source['source_id']}
    except KeyError:
        raise ValueError('固定费率表未覆盖该站点、部门或仓配') from None
