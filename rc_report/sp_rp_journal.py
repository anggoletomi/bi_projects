from dotenv import load_dotenv
load_dotenv()

import sys,os
sys.path.insert(0, os.getenv("PROJECT_PATH"))

from bi_function import *
from rc_report.rc_setup import *

def transform_wallet_data(df):

    df_copy = df.copy()

    df_copy['wallet_category'] = df_copy['description'].apply(flexible_categorize_by_description, args=(shopee_wallet_category_mappings, 'database', 'flexible'))
    df_copy.drop(columns=['transaction_type','description','transaction_category','ending_balance','status'], inplace=True, errors='ignore')

    pivot_group = ['folder_id', 'transaction_date', 'order_number']
    df_pivot = df_copy.pivot_table(index=pivot_group, columns='wallet_category', values='amount', aggfunc='sum', fill_value=0).reset_index()
    df_pivot.columns.name = None

    # Process Wallet Data - Validate Pivot Result 1
    def validate_pivot(df_original,df_original_column_ref,df_pivot):
        original_sum = df_original[df_original_column_ref].sum()
        pivot_sum = sum(df_pivot[col].sum() for col in [col for col in df_pivot.columns if col not in pivot_group])

        if original_sum != pivot_sum:
            print("\033[91m\033[1m❗️🚨 ALERT: Original sum ({}) does not match Pivot sum ({})! 🚨❗️\033[0m".format(original_sum, pivot_sum))

    validate_pivot(df,'amount',df_pivot)

    # Process Wallet Data - Split Duplicate Wallet
    duplicates_mask = df_pivot.duplicated(subset=['folder_id', 'order_number'], keep=False)

    df_pivot_unique = df_pivot[~duplicates_mask]
    df_pivot_duplicates = df_pivot[duplicates_mask]

    # Process Wallet Data - Group Wallet Duplicates That Has Order Number

    df_pivot_duplicates_with_order = df_pivot_duplicates[df_pivot_duplicates['order_number'].str.len() > 1]

    aggregation_functions = {
        'transaction_date': 'min', # Take the earliest transaction_date
    }

    for col in df_pivot_duplicates_with_order.columns: # Dynamically add 'sum' for any other columns except the fixed ones
        if col not in pivot_group:
            aggregation_functions[col] = 'sum'

    df_pivot_duplicates_with_order_grouped = df_pivot_duplicates_with_order.groupby(['folder_id','order_number']).agg(aggregation_functions).reset_index()

    # Process Wallet Data - Combine All Pivot Data

    final_pivot = pd.concat([df_pivot_unique,df_pivot_duplicates_with_order_grouped])

    validate_pivot(df,'amount',final_pivot)

    return final_pivot

def create_journal_base(journal_base=True,data_month=None,folder_id=None,start_date=None,db_method='append',transform=True):

    """ 
    Description:
    -----------
    - If `journal_base` is set to True, the `data_month` and `folder_id` fields are mandatory.
        This mode processed the data monthly for each store, matching transactions based on the month.
        This setup is intended to generate a monthly journal report, allowing us to track which funds have been withdrawn and which are still pending.
    - If `journal_base` is set to False, the `start_date` fields are mandatory.
        This mode tracks each order across time periods, providing a comprehensive view at the order level only.
        The data is matched based on the order number, regardless of whether the funds were released,
        or the wallet transaction occurred in a different month.   
    """

    if journal_base:
        print(f"\033[1;32m🚀 Process Journal for : {folder_id}, month = {data_month} 🚀\033[0m")

        query_order = f'''SELECT DATE(order_creation_time) AS order_creation_time,folder_id,order_number,
                            SUM(total_product_price) AS total_product_price
                    FROM `bi-gbq.rc_report.sp_order_data`
                    WHERE (UPPER(order_status) = 'SELESAI')
                        AND (LENGTH(order_number) > 1)
                        AND (month_order = '{data_month}')
                        AND (folder_id = '{folder_id}')
                    GROUP BY DATE(order_creation_time),folder_id,order_number
        '''

        query_income = f'''SELECT *
                            FROM `bi-gbq.rc_report.sp_income_released`
                            WHERE (month_income = '{data_month}')
                                AND (month_order = '{data_month}')
                                AND (LENGTH(order_number) > 1)
                                AND (folder_id = '{folder_id}')
        '''

        query_wallet = f'''SELECT *
                            FROM `bi-gbq.rc_report.sp_pay_wallet`
                            WHERE (month_wallet = '{data_month}')
                                AND (folder_id = '{folder_id}')
                            ORDER BY transaction_date DESC
        '''
    
    elif not journal_base:
        print(f"\033[1;32m📋 Combined All Order, Income & Wallet Data 📋\033[0m")

        today_date = datetime.today().strftime('%Y-%m-%d')

        query_order = f'''SELECT DATE(order_creation_time) AS order_creation_time,folder_id,order_number,order_status,
                                SUM(total_product_price) AS total_product_price
                            FROM `bi-gbq.rc_report.sp_order_data`
                            WHERE (DATE(order_creation_time) BETWEEN '{start_date}' AND '{today_date}')
                                AND (LENGTH(order_number) > 1)
                            GROUP BY DATE(order_creation_time),folder_id,order_number,order_status'''

        query_income = f'''SELECT * FROM `bi-gbq.rc_report.sp_income_released`
                           WHERE (DATE(order_creation_time) BETWEEN '{start_date}' AND '{today_date}')
                                AND (LENGTH(order_number) > 1)'''

        query_wallet = f'''SELECT * FROM `bi-gbq.rc_report.sp_pay_wallet`
                           WHERE (DATE(transaction_date) BETWEEN '{start_date}' AND '{today_date}')
                                AND (LENGTH(order_number) > 1)
                            ORDER BY transaction_date DESC'''

    df_order = read_from_gbq(BI_CLIENT,query_order)
    df_income = read_from_gbq(BI_CLIENT,query_income)
    df_wallet = read_from_gbq(BI_CLIENT,query_wallet)

    if len(df_order) == 0 or len(df_income) == 0 or len(df_wallet) == 0:
        empty_dfs = []
        if len(df_order) == 0:
            empty_dfs.append("df_order")
        if len(df_income) == 0:
            empty_dfs.append("df_income")
        if len(df_wallet) == 0:
            empty_dfs.append("df_wallet")
            
        print(f"Skip creating journal stage: {', '.join(empty_dfs)} data is empty")
        print('-------------------------------------------------------------------------------')
        return
    
    # Drop Dimension Column
    columns_to_drop = ['month_order', 'month_income', 'month_wallet', 'store_id', 'country', 'currency', 'platform', 'store']
    df_order = df_order.drop(columns=[col for col in columns_to_drop if col in df_order.columns])
    df_income = df_income.drop(columns=[col for col in columns_to_drop if col in df_income.columns])
    df_wallet = df_wallet.drop(columns=[col for col in columns_to_drop if col in df_wallet.columns])

    # Localize Time
    def localize_time(df,col_name):
        df[col_name] = pd.to_datetime(df[col_name]).dt.tz_localize(None)

    localize_time(df_order,'order_creation_time')
    localize_time(df_income,'order_creation_time')
    localize_time(df_income,'fund_release_date')
    localize_time(df_wallet,'transaction_date')

    # Handling Error Duplicate 'Penarikan Dana' in Wallet Data
    # Convert transaction_date to datetime and extract the date
    df_wallet['transaction_date_only'] = pd.to_datetime(df_wallet['transaction_date']).dt.date

    # Identify duplicates only for 'Penarikan Dana'
    mask = (df_wallet['transaction_type'] == 'Penarikan Dana')

    # Drop duplicates based on the specified columns, keeping the last occurrence
    df_wallet = df_wallet[~mask | ~df_wallet.duplicated(subset=['folder_id', 'transaction_date_only', 'amount'], keep='last')]
    df_wallet = df_wallet.drop(columns=['transaction_date_only'])

    # Prepare to Merge
    if transform:
        key_columns = ['folder_id', 'order_number']
        df_wallet = transform_wallet_data(df_wallet)

    elif not transform:
        # ADD HELPER COLUMN FOR MERGE, SO IT CAN BE MERGE UNIQUE ORDER ONLY
        df_order['merge_helper'] = 1 # all 1 because df_order order number is already unique
        df_income['merge_helper'] = 1 # all 1 because df_income order number is already unique

        keywords = ['penghasilan dari pesanan', 'kompensasi kehilangan']
        df_wallet['merge_helper'] = df_wallet['description'].apply(lambda x: 1 if any(keyword.lower() in x.lower() for keyword in keywords) else 0)

        # Add an index to the column name to differentiate the origin of each column after merging
        key_columns = ['folder_id', 'order_number', 'merge_helper']

    df_order = df_order.rename(columns={col: f'o_{col}' for col in df_order.columns if col not in key_columns})
    df_income = df_income.rename(columns={col: f'i_{col}' for col in df_income.columns if col not in key_columns})
    df_wallet = df_wallet.rename(columns={col: f'w_{col}' for col in df_wallet.columns if col not in key_columns})

    if journal_base:
        # ADD FLAG STATUS - WALLET DATA
        try:
            first_appearance_idx = df_wallet[df_wallet['w_description'].str.contains('penarikan dana', case=False)].index[0]

            df_wallet['wp_has_been_withdrawn'] = 1 # Set default values
            df_wallet.loc[:first_appearance_idx-1, 'wp_has_been_withdrawn'] = 0

            # Handle Wallet Data Where It's Not Full Withdrawn Indicated by Ending Balance is not 0
            ending_balance_check = df_wallet.loc[[first_appearance_idx]]['w_ending_balance'].iloc[0]
            cumulative_sum = 0 # If ending_balance_check > 0, find cumulative rows below
            if ending_balance_check > 0:
                for idx in range(first_appearance_idx + 1, len(df_wallet)):
                    cumulative_sum += df_wallet.loc[idx, 'w_amount']
                    if cumulative_sum == ending_balance_check:
                        # Set 'wp_has_been_withdrawn' to 0 for all rows contributing to the cumulative sum
                        df_wallet.loc[first_appearance_idx + 1:idx, 'wp_has_been_withdrawn'] = 0
                        break
        except IndexError:
            # If no 'penarikan dana' is found, set 'wp_has_been_withdrawn' to 0 for all rows
            df_wallet['wp_has_been_withdrawn'] = 0

        df_wallet['wp_this_month_order'] = df_wallet['order_number'].apply(lambda x: 1 if x in df_order['order_number'].values else 0)

        df_wallet['wp_described_as_income'] = df_wallet['merge_helper']

    # MERGE ALL TABLE

    df_order_income = df_order.merge(df_income, on=key_columns, how='outer', indicator='merge_status_1')
    df_order_income['merge_status_1'] = df_order_income['merge_status_1'].replace({
        'left_only': 'ORDER',
        'right_only': 'INCOME',
        'both': 'ORDER,INCOME'
    })

    df_order_income_wallet = df_order_income.merge(df_wallet, on=key_columns, how='outer', indicator='merge_status_2')

    conditions = [
        df_order_income_wallet['merge_status_2'] == 'right_only',
        df_order_income_wallet['merge_status_2'] == 'left_only',
        df_order_income_wallet['merge_status_2'] == 'both'
    ]

    # Define the corresponding values for each condition
    choices = [
        'WALLET',  # when merge_status_2 is 'right_only'
        df_order_income_wallet['merge_status_1'].astype(str),  # when merge_status_2 is 'left_only'
        df_order_income_wallet['merge_status_1'].astype(str) + ',WALLET'  # when merge_status_2 is 'both'
    ]

    # Available Merge Status:
    # ORDER
    # ORDER,INCOME
    # ORDER,WALLET
    # ORDER,INCOME,WALLET
    # INCOME
    # INCOME,WALLET
    # WALLET

    # Apply the conditions with np.select
    df_order_income_wallet['merge_status'] = np.select(conditions, choices, default=None)

    # Drop Column

    df_order_income_wallet.drop(columns=['merge_status_1', 'merge_status_2'], inplace=True)

    if journal_base:
        # in excel video tutorial, sheetname : all order
        df_order_income_wallet['sheet_omset'] = np.where(
            (df_order_income_wallet['merge_status'].isin(['ORDER', 'ORDER,WALLET', 'ORDER,INCOME', 'ORDER,INCOME,WALLET'])) &
            (df_order_income_wallet['wp_described_as_income'] != 0),
            1,
            0
        )

        # in excel video tutorial, sheetname : wp part 1, wp part 2, wp part 3
        df_order_income_wallet['sheet_wp'] = np.where(
            (df_order_income_wallet['merge_status'].isin(['WALLET', 'ORDER,WALLET', 'INCOME,WALLET', 'ORDER,INCOME,WALLET'])) &
            (df_order_income_wallet['wp_this_month_order'] == 1) &
            (df_order_income_wallet['wp_described_as_income'] == 1) &
            (df_order_income_wallet['wp_has_been_withdrawn'] == 1),
            1,
            0
        )

        # in excel video tutorial, sheetname : piutang
        df_order_income_wallet['sheet_piutang'] = np.where(
            (df_order_income_wallet['merge_status'].isin(['ORDER', 'ORDER,WALLET', 'ORDER,INCOME', 'ORDER,INCOME,WALLET'])) &
            (df_order_income_wallet['wp_has_been_withdrawn'] != 1),
            1,
            0
        )

    if transform:
        # Clean Data Type
        date_col = ['o_order_creation_time','i_order_creation_time','i_fund_release_date','w_transaction_date']
        str_col = ['folder_id','order_number','o_order_status','i_index','i_submission_number','i_buyer_username',
                'i_buyer_payment_method','i_voucher_code','i_courier_service','i_courier_name','i_unnamed_column_33',
                'merge_status']

        for d in date_col:
            df_order_income_wallet[d] = pd.to_datetime(df_order_income_wallet[d])

        for col in df_order_income_wallet.columns:
            if col not in date_col and col not in str_col:
                df_order_income_wallet[col] = df_order_income_wallet[col].fillna(0).astype(float)

    # Add Dimension

    df_order_income_wallet.insert(0, 'month_order', df_order_income_wallet['o_order_creation_time'].dt.strftime('%Y%m'))
    df_order_income_wallet.insert(1, 'month_income', df_order_income_wallet['i_fund_release_date'].dt.strftime('%Y%m'))
    df_order_income_wallet.insert(2, 'month_wallet', df_order_income_wallet['w_transaction_date'].dt.strftime('%Y%m'))

    if journal_base:

        df_order_income_wallet.insert(3, 'report_month', data_month)

        df_order_income_wallet.insert(4, 'store_id', shopee_store_info[folder_id][0])
        df_order_income_wallet.insert(5, 'country', shopee_store_info[folder_id][1])
        df_order_income_wallet.insert(6, 'currency', shopee_store_info[folder_id][2])
        df_order_income_wallet.insert(7, 'platform', shopee_store_info[folder_id][3])
        df_order_income_wallet.insert(8, 'store', shopee_store_info[folder_id][4])

    else:

        def add_dim(column_name, dict_position, insert_at=0):
            # Map the dictionary value at the specified position
            new_column = df_order_income_wallet['folder_id'].map(
                lambda x: shopee_store_info.get(x, [None] * (dict_position + 1))[dict_position]
            )
            # Insert the new column at the desired location
            df_order_income_wallet.insert(insert_at, column_name, new_column)

        add_dim(column_name='store_id',dict_position=0,insert_at=3)
        add_dim(column_name='country',dict_position=1,insert_at=4)
        add_dim(column_name='currency',dict_position=2,insert_at=5)
        add_dim(column_name='platform',dict_position=3,insert_at=6)
        add_dim(column_name='store',dict_position=4,insert_at=7)

    # Load to GBQ
    
    if journal_base:
        write_table_by_unique_id(df_order_income_wallet,
                                target_table = 'rc_report.rpt_sp_journal_base',
                                write_method=db_method,
                                unique_col_ref = ['report_month','folder_id']
                                )
    elif not journal_base and not transform:
        write_table_by_unique_id(df_order_income_wallet,
                                 target_table = 'rc_report.rpt_sp_journal_order',
                                 write_method=db_method,
                                 unique_col_ref = ['order_number','folder_id']
                                )
    elif not journal_base and transform:
        write_table_by_unique_id(df_order_income_wallet,
                                target_table = 'rc_report.rpt_sp_journal_order_transform',
                                write_method=db_method,
                                unique_col_ref = ['order_number','folder_id']
                                )
        
def check_previous_wallet_with_no_withdrawn_at_all_in_month(report_month,folder_id,include_current_month=True):

    n = 12 # how many last month to take
    prev_month_list = [(datetime.strptime(report_month, "%Y%m").replace(day=1) - timedelta(days=30 * i)).strftime("%Y%m") for i in range(1, n+1)]

    previous_month = (datetime.strptime(report_month, '%Y%m').replace(day=1) - timedelta(days=1)).strftime('%Y%m')

    query = f'''WITH all_month_wallet AS (
        SELECT DISTINCT month_wallet
        FROM `bi-gbq.rc_report.sp_pay_wallet`
        WHERE folder_id = '{folder_id}'
        AND month_wallet IN {tuple(prev_month_list)}
    ),
    month_with_penarikan_dana AS (
        SELECT DISTINCT month_wallet, 'Yes' AS penarikan_dana_flag
        FROM `bi-gbq.rc_report.sp_pay_wallet`
        WHERE folder_id = '{folder_id}'
        AND LOWER(description) LIKE '%penarikan dana%'
    )
    SELECT 
        a.month_wallet,
        COALESCE(m.penarikan_dana_flag, 'No') AS penarikan_dana_flag
    FROM all_month_wallet AS a
    LEFT JOIN month_with_penarikan_dana AS m
    ON a.month_wallet = m.month_wallet
    ORDER BY a.month_wallet'''

    df = read_from_gbq(BI_CLIENT,query)

    # Initialize an empty list to store the result
    result = []
    
    # Start from the last row and move upwards
    for index in range(len(df) - 1, -1, -1):
        # Get the current row values
        row = df.iloc[index]
        month_wallet = row['month_wallet']
        penarikan_dana_flag = row['penarikan_dana_flag']
        
        # Check the penarikan_dana_flag
        if penarikan_dana_flag == 'Yes':
            # If 'Yes' is encountered, stop the operation
            break
        elif penarikan_dana_flag == 'No':
            # Append the current month_wallet to the result
            result.append(month_wallet)
    
    if len(result) == 0:
        if include_current_month:
            return report_month
        else:
            return previous_month
    else:
        min_month = min(result)
        prev_min_month = (datetime.strptime(min_month, '%Y%m').replace(day=1) - timedelta(days=1)).strftime('%Y%m')
        result.append(prev_min_month)
        if include_current_month:
            result.append(report_month)
        return tuple(result)
        
def withdrawn_last_month(report_month,folder_id):

    no_withdrawn_in_a_month = check_previous_wallet_with_no_withdrawn_at_all_in_month(report_month,folder_id)
    before_this_month_excluded = [(datetime.strptime(report_month, "%Y%m").replace(day=1) - timedelta(days=30 * i)).strftime("%Y%m") for i in range(1, 13)]
    before_this_month_included = [(datetime.strptime(report_month, "%Y%m").replace(day=1) - timedelta(days=30 * i)).strftime("%Y%m") for i in range(0, 13)]

    if isinstance(no_withdrawn_in_a_month, str):
        month_wallet_condition = f"month_wallet = '{no_withdrawn_in_a_month}'"
    elif isinstance(no_withdrawn_in_a_month, tuple):
        month_wallet_condition = f"month_wallet IN {tuple(no_withdrawn_in_a_month)}"

    # We use a list of months instead of only the previous month because there are cases
    # where an order is recorded in the wallet as sales after a delay.
    # For example, an order might be created in May 2024, with no wallet data for it in May 2024 or June 2024,
    # but it only appears in the wallet data in July 2024. Therefore, we need to check 'Piutang' (accounts receivable) from the last few months.

    query = f'''
        WITH order_income AS (
            SELECT *
            FROM `bi-gbq.rc_report.rpt_sp_journal_order` AS income_data
            WHERE month_income IN {tuple(before_this_month_included)}
            AND folder_id = '{folder_id}'
            AND EXISTS (
                SELECT 1
                FROM `bi-gbq.rc_report.rpt_sp_journal_base` AS order_data
                WHERE order_data.folder_id = '{folder_id}'
                    AND order_data.order_number = income_data.order_number
                    AND order_data.month_order IN {tuple(before_this_month_excluded)}
                    AND order_data.sheet_piutang = 1
                    AND order_data.folder_id = income_data.folder_id
            )
        ),
        wallet_data AS (
            SELECT folder_id, order_number, wp_has_been_withdrawn, wp_this_month_order, wp_described_as_income,
                    sheet_omset, sheet_wp, sheet_piutang
            FROM `bi-gbq.rc_report.rpt_sp_journal_base`
            WHERE {month_wallet_condition}
            AND wp_described_as_income = 1
            AND folder_id = '{folder_id}'
            AND sheet_piutang = 0
        )
        SELECT 
            order_income.*, 
            wallet_data.wp_has_been_withdrawn,
            wallet_data.wp_this_month_order,
            wallet_data.wp_described_as_income,
            wallet_data.sheet_omset,
            wallet_data.sheet_wp,
            wallet_data.sheet_piutang
        FROM 
            order_income
        INNER JOIN
            wallet_data
        ON 
            order_income.folder_id = wallet_data.folder_id
            AND order_income.order_number = wallet_data.order_number
    '''
    
    try:
        df = read_from_gbq(BI_CLIENT, query)
        
        if df.empty: # Check if the DataFrame is empty
            print(f"⚠️ withdrawn_last_month : no data found for the previous month ({no_withdrawn_in_a_month}) and folder index ({folder_id})")
            print('-------------------------------------------------------------------------------')
            return pd.DataFrame()
    except Exception as e:
        print(f"⚠️ withdrawn_last_month : failed to read data due to error: {e}")
        print('-------------------------------------------------------------------------------')
        return pd.DataFrame()

    # Localize Time
    for d in ['o_order_creation_time','i_order_creation_time','i_fund_release_date','w_transaction_date']:
        df[d] = df[d].dt.tz_localize(None)

    # Add Report Month
    df['report_month'] = report_month

    # Further processing for multiple wallet (no_withdrawn_in_a_month is a tuple)

    if isinstance(no_withdrawn_in_a_month, tuple):
        earliest_month_wallet = min(no_withdrawn_in_a_month)

        df_earliest = df[(df['month_wallet'] == earliest_month_wallet) & (df['sheet_piutang'] == 1)]
        df_others = df[df['month_wallet'] != earliest_month_wallet]

        df = pd.concat([df_earliest, df_others], ignore_index=True)

    # Arrange Column Order

    journal_base_raw_col_list = read_from_gbq(BI_CLIENT,'SELECT * FROM `bi-gbq.rc_report.rpt_sp_journal_base` LIMIT 1')

    df_filtered = df[journal_base_raw_col_list.columns.tolist()]

    df_filtered['idx_sheet_temp'] = 1 # so we can align with calculate_debit_credit function later

    return df_filtered

def pending_last_month(report_month,folder_id,month_col_ref):

    month_list = check_previous_wallet_with_no_withdrawn_at_all_in_month(report_month,folder_id,include_current_month=False)

    if isinstance(month_list, str):
        month_wallet_condition = f"month_wallet = '{month_list}'"
    elif isinstance(month_list, tuple):
        month_wallet_condition = f"month_wallet IN {tuple(month_list)}"

    # Check whether in the report_month there is a withdrawal activity
    count_withdrawal = read_from_gbq(BI_CLIENT,f'''SELECT count(1) FROM `bi-gbq.rc_report.rpt_sp_journal_base`
    WHERE month_wallet = '{report_month}' AND folder_id = '{folder_id}'
    AND LOWER(w_description) LIKE '%penarikan dana%' ''')

    if count_withdrawal.iloc[0, 0] == 0:
        return pd.DataFrame()
    
    query = f'''SELECT * FROM `bi-gbq.rc_report.rpt_sp_journal_base`
                WHERE ({month_wallet_condition})
                      AND (wp_has_been_withdrawn = 0)
                      AND (folder_id = '{folder_id}')'''
    
    try:
        df = read_from_gbq(BI_CLIENT, query)
        # Check if the DataFrame is empty
        if df.empty:
            print(f"⚠️ pending_last_month : no data found for the previous month ({month_list}), folder index ({folder_id})")
            print('-------------------------------------------------------------------------------')
            return pd.DataFrame()
    except Exception as e:
        print(f"⚠️ pending_last_month : failed to read data due to error: {e}")
        print('-------------------------------------------------------------------------------')
        return pd.DataFrame()

    # Localize Time
    for d in ['o_order_creation_time','i_order_creation_time','i_fund_release_date','w_transaction_date']:
        df[d] = df[d].dt.tz_localize(None)

    # Add Report Month
    df['report_month'] = report_month # update the report_month because this going to be use as journal this month

    # Filter Data

    df = df[df[month_col_ref].notnull()]

    # Arrange Column Order

    journal_base_raw_col_list = read_from_gbq(BI_CLIENT,'SELECT * FROM `bi-gbq.rc_report.rpt_sp_journal_base` LIMIT 1')

    df_filtered = df[journal_base_raw_col_list.columns.tolist()]

    df_filtered['idx_sheet_temp'] = 1 # so we can align with calculate_debit_credit function later

    return df_filtered

def calculate_debit_credit(df,col_filter,col_list_to_sum,sort_index,category_name=None,wallet_category=False):

    df_filter = df[df[col_filter] == 1]

    df_filter['value_withdrawn'] = np.where(df_filter['sheet_piutang'] == 1,0,df_filter[col_list_to_sum].sum(axis=1))
    df_filter['value_pending'] = np.where(df_filter['sheet_piutang'] == 0,0,df_filter[col_list_to_sum].sum(axis=1))
    df_filter['value_total'] = df_filter['value_withdrawn'] + df_filter['value_pending']

    df_filter['value_debit'] = np.where(df_filter['value_total'] >= 0,df_filter['value_total'],0)
    df_filter['value_credit'] = np.where(df_filter['value_total'] < 0,df_filter['value_total'],0)

    df_filter = df_filter[df_filter['value_total'] != 0]

    if wallet_category:
        df_filter['category_1'] = df_filter['w_description'].apply(flexible_categorize_by_description, args=(shopee_wallet_category_mappings, 'simple', 'flexible'))
        df_filter = df_filter[df_filter['category_1'] != 'Penghasilan Dari Pesanan']
        df_filter['category_1'] = df_filter['category_1'] + ' (W)'

    else:
        df_filter['category_1'] = category_name
    
    df_filter['sort_index'] = sort_index

    return df_filter

def create_journal_dashboard(report_month,folder_id,db_method='append'):

    print(f"\033[1;32m📊 Journal Dashboard for : {folder_id}, month = {report_month} 📊\033[0m")

    query = f'''SELECT * FROM `bi-gbq.rc_report.rpt_sp_journal_base`
                WHERE report_month = '{report_month}' AND folder_id = '{folder_id}'
            '''

    df_base = read_from_gbq(BI_CLIENT,query)

    if not len(df_base) > 0:
        print(f"Skip creating journal dashboard: journal base data is empty")
        print('-------------------------------------------------------------------------------')
        return

    df1 = calculate_debit_credit(df_base,col_filter='sheet_omset',col_list_to_sum=['o_total_product_price'],sort_index=1,category_name='Penjualan Kotor (O)')
    df2 = calculate_debit_credit(df_base,col_filter='sheet_wp',col_list_to_sum=['i_buyer_refund_amount'],sort_index=2,category_name='Pengembalian Dana (I)')
    df3 = calculate_debit_credit(df_base,col_filter='sheet_wp',col_list_to_sum=['i_shopee_product_discount'],sort_index=3,category_name='Diskon Produk Dari Shopee (I)')

    df4 = calculate_debit_credit(df_base,col_filter='sheet_wp',col_list_to_sum=['i_seller_borne_voucher_discount',
                                                                                                'i_seller_borne_cashback_coins',
                                                                                                'i_shipping_paid_by_buyer',
                                                                                                'i_shipping_discount_borne_by_courier',
                                                                                                'i_shopee_free_shipping',
                                                                                                'i_shipping_fees_forwarded_to_courier',
                                                                                                'i_return_shipping_cost',
                                                                                                'i_shipping_fee_refund'],sort_index=4,category_name='Beban Ongkir (I)')

    df5 = calculate_debit_credit(df_base,col_filter='sheet_wp',col_list_to_sum=['i_ams_commission_fee'],sort_index=5,category_name='Biaya AMS (I)')
    df6 = calculate_debit_credit(df_base,col_filter='sheet_wp',col_list_to_sum=['i_administration_fee'],sort_index=6,category_name='Biaya Admin (I)')
    df7 = calculate_debit_credit(df_base,col_filter='sheet_wp',col_list_to_sum=['i_service_fee_incl_vat_11_percent'],sort_index=7,category_name='Biaya Layanan (I)')
    df8 = calculate_debit_credit(df_base,col_filter='sheet_wp',col_list_to_sum=['i_program_fee'],sort_index=8,category_name='Biaya Program (I)')
    df9 = calculate_debit_credit(df_base,col_filter='wp_has_been_withdrawn',col_list_to_sum=['w_amount'],sort_index=9,wallet_category=True)

    ######################################################################################################

    def reverse(df_debit_credit,sort_index,category_name):
        df = df_debit_credit.copy()

        df['value_withdrawn'] = 0
        df['value_pending'] = df['value_pending'] * -1
        df['value_total'] = df['value_withdrawn'] + df['value_pending']

        df['value_debit'] = np.where(df['value_total'] >= 0,df['value_total'],0)
        df['value_credit'] = np.where(df['value_total'] < 0,df['value_total'],0)

        df = df[df['value_total'] != 0]

        df['category_1'] = category_name
        df['sort_index'] = sort_index

        return df

    df10 = reverse(df1,sort_index=10,category_name='Piutang (O)')

    ######################################################################################################

    pending_last_month_income = pending_last_month(report_month,folder_id,month_col_ref='month_income')
    withdrawn_last_month_income = withdrawn_last_month(report_month,folder_id)
    
    last_month_income = pd.concat([withdrawn_last_month_income,pending_last_month_income])

    if len(last_month_income) > 0:

        df11 = calculate_debit_credit(last_month_income,col_filter='idx_sheet_temp',col_list_to_sum=['i_original_product_price','i_total_product_discount'],sort_index=11,category_name='Previous Month - Piutang (I)')

        df12 = calculate_debit_credit(last_month_income,col_filter='idx_sheet_temp',col_list_to_sum=['i_buyer_refund_amount'],sort_index=12,category_name='Previous Month - Pengembalian Dana (I)')
        df13 = calculate_debit_credit(last_month_income,col_filter='idx_sheet_temp',col_list_to_sum=['i_shopee_product_discount'],sort_index=13,category_name='Previous Month - Diskon Produk Dari Shopee (I)')

        df14 = calculate_debit_credit(last_month_income,col_filter='idx_sheet_temp',col_list_to_sum=['i_seller_borne_voucher_discount',
                                                                                                    'i_seller_borne_cashback_coins',
                                                                                                    'i_shipping_paid_by_buyer',
                                                                                                    'i_shipping_discount_borne_by_courier',
                                                                                                    'i_shopee_free_shipping',
                                                                                                    'i_shipping_fees_forwarded_to_courier',
                                                                                                    'i_return_shipping_cost',
                                                                                                    'i_shipping_fee_refund'],sort_index=14,category_name='Previous Month - Beban Ongkir (I)')

        df15 = calculate_debit_credit(last_month_income,col_filter='idx_sheet_temp',col_list_to_sum=['i_ams_commission_fee'],sort_index=15,category_name='Previous Month - Biaya AMS (I)')
        df16 = calculate_debit_credit(last_month_income,col_filter='idx_sheet_temp',col_list_to_sum=['i_administration_fee'],sort_index=16,category_name='Previous Month - Biaya Admin (I)')
        df17 = calculate_debit_credit(last_month_income,col_filter='idx_sheet_temp',col_list_to_sum=['i_service_fee_incl_vat_11_percent'],sort_index=17,category_name='Previous Month - Biaya Layanan (I)')
        df18 = calculate_debit_credit(last_month_income,col_filter='idx_sheet_temp',col_list_to_sum=['i_program_fee'],sort_index=18,category_name='Previous Month - Biaya Program (I)')
    else:
        df11 = df12 = df13 = df14 = df15 = df16 = df17 = df18 = pd.DataFrame()
    
    ######################################################################################################

    pending_last_month_wallet = pending_last_month(report_month,folder_id,month_col_ref='month_wallet')

    if len(pending_last_month_wallet) > 0:

        temp_pending_last_month_wallet = pending_last_month_wallet.copy()
        temp_pending_last_month_wallet['sheet_piutang_temp'] = temp_pending_last_month_wallet['sheet_piutang']
        temp_pending_last_month_wallet['sheet_piutang'] = 1 # make all one first to be able to use calculate_debit_credit function

        df19 = calculate_debit_credit(temp_pending_last_month_wallet,col_filter='idx_sheet_temp',col_list_to_sum=['w_amount'],sort_index=19,wallet_category=True)

        df19['category_1'] = 'Previous Month - ' + df19['category_1']

        df19['sheet_piutang'] = df19['sheet_piutang_temp']
        df19.drop(columns=['sheet_piutang_temp'], inplace=True)

    else:
        df19 = pd.DataFrame()
        
    ######################################################################################################

    for x in [df11, df12, df13, df14, df15, df16, df17, df18, df19]:
        x.drop(columns=['idx_sheet_temp'], inplace=True, errors='ignore')

    df_concat = pd.concat([df1, df2, df3, df4, df5, df6, df7, df8, df9, df10, df11, df12, df13, df14, df15, df16, df17, df18, df19], ignore_index=True)

    for d in ['o_order_creation_time', 'i_order_creation_time', 'i_fund_release_date', 'w_transaction_date']:
        df_concat[d] = pd.to_datetime(df_concat[d], errors='coerce').dt.tz_localize(None)

    # Load to GBQ
    
    write_table_by_unique_id(df_concat,
                                target_table = 'rc_report.rpt_sp_journal_dashboard',
                                write_method=db_method,
                                unique_col_ref = ['report_month','folder_id']
                            )

if __name__ == '__main__':

    tasks = [
        # 1. Create Journal Order
        (create_journal_base, {'journal_base': False, 'start_date': '2024-01-01', 'db_method': 'replace', 'transform' : False}),

        # 2. Create Journal Order Transform
        (create_journal_base, {'journal_base': False, 'start_date': '2024-01-01', 'db_method': 'replace', 'transform' : True}),
    ]

    # 3. Create Journal Base (looped)
    for folder in shopee_store_info.keys():
        for month in ['202401', '202402', '202403', '202404', '202405', '202406', '202407','202408','202409','202410','202411','202412']:
            tasks.append((create_journal_base, {'journal_base': True, 'data_month': month, 'folder_id': folder, 'db_method': 'append', 'transform' : False}))

    # 4. Create Journal Dashboard (looped)
    for folder in shopee_store_info.keys():
        for month in ['202401', '202402', '202403', '202404', '202405', '202406', '202407','202408','202409','202410','202411','202412']:
            tasks.append((create_journal_dashboard, {'report_month': month, 'folder_id': folder, 'db_method': 'append'}))

    # Execute all tasks using log_function
    log_function(tasks)