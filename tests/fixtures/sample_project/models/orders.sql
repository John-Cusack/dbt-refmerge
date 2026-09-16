with orders as (
    select
        order_id,
        customer_id
    from {{ ref('stg_orders') }}
),
order_financials as (
    select
        order_id,
        amount
    from {{ ref('stg_orders') }}
),
final as (
    select orders.order_id, orders.customer_id, order_financials.amount
    from orders
    join order_financials on orders.order_id = order_financials.order_id
)
select * from final
