with orders as (

    select
        order_id,
        customer_id,
        amount

    from {{ ref('stg_orders') }}

),

final as (

    select
        orders.order_id,
        order_financials.amount

    from orders
    join orders as order_financials
        on orders.order_id = order_financials.order_id

)

select * from final
